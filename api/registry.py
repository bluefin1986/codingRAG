"""PostgreSQL-backed document registry for codingRAG v2 Phase 1."""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import (
    CODING_RAG_DATABASE_URL,
    CODING_RAG_IMPORT_BATCH_SIZE,
    CODING_RAG_SEAWEEDFS_BUCKET,
    CODING_RAG_SEAWEEDFS_FILER_URL,
    CODING_RAG_SEAWEEDFS_KEY_PREFIX,
    CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
    CODING_RAG_STORAGE_BACKEND,
    DOMAIN_REGISTRY,
    get_domain_config,
)
from api.storage import create_storage

TEXT_EXTENSIONS = {".md", ".markdown", ".mdx", ".txt", ".html", ".htm", ".rst"}
DEFAULT_RETENTION_VERSIONS = 2
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanResult:
    scan_run_id: str
    domain: str
    library_id: str
    scanned: int
    created: int
    changed: int
    unchanged: int
    skipped: int


class RegistryUnavailable(RuntimeError):
    pass


class DocumentRegistry:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or CODING_RAG_DATABASE_URL
        if not self.database_url:
            raise RegistryUnavailable("CODING_RAG_DATABASE_URL is not configured")

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                cur.execute(MIGRATION_SQL)

    def list_libraries(self) -> list[dict[str, Any]]:
        self.init_schema()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.*,
                       COALESCE(COUNT(d.id), 0)::int AS document_count,
                       COALESCE(COUNT(d.id) FILTER (WHERE d.enabled), 0)::int AS enabled_document_count
                FROM doc_libraries l
                LEFT JOIN documents d ON d.library_id = l.id AND d.deleted_at IS NULL
                GROUP BY l.id
                ORDER BY l.code
                """
            )
            return list(cur.fetchall())

    def scan_domain(self, domain: str, *, limit: int | None = None) -> ScanResult:
        domain = domain.strip().lower()
        if domain not in DOMAIN_REGISTRY:
            raise ValueError(f"Unknown domain={domain!r}; available: {sorted(DOMAIN_REGISTRY)}")
        self.init_schema()

        cfg = get_domain_config(domain)
        docs_dir = Path(cfg["docs_dir"]).expanduser().resolve()
        if not docs_dir.exists():
            raise FileNotFoundError(f"docs_dir does not exist for domain={domain}: {docs_dir}")

        storage = create_storage(
            CODING_RAG_STORAGE_BACKEND,
            seaweedfs_filer_url=CODING_RAG_SEAWEEDFS_FILER_URL,
            seaweedfs_public_base_url=CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
            seaweedfs_bucket=CODING_RAG_SEAWEEDFS_BUCKET,
            seaweedfs_key_prefix=CODING_RAG_SEAWEEDFS_KEY_PREFIX,
        )
        scan_run_id = str(uuid.uuid4())
        library_id = str(uuid.uuid4())
        scanned = created = changed = unchanged = skipped = 0

        files = iter_document_files(docs_dir)
        if limit is not None:
            files = list(files)[: max(limit, 0)]

        with self._connect() as conn, conn.cursor() as cur:
            library_id = self._upsert_library(cur, domain, cfg, docs_dir)
            for path in files:
                try:
                    metadata = inspect_document(path, docs_dir, domain, cfg)
                    stored = storage.put_existing_file(path, relative_path=metadata["relative_path"])
                    action = self._upsert_document(cur, library_id, metadata, stored, scan_run_id)
                    scanned += 1
                    if action == "created":
                        created += 1
                    elif action == "changed":
                        changed += 1
                    else:
                        unchanged += 1
                except Exception:
                    skipped += 1
            conn.commit()

        return ScanResult(scan_run_id, domain, library_id, scanned, created, changed, unchanged, skipped)

    def list_documents(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.init_schema()
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        clauses = ["d.deleted_at IS NULL"]
        params: list[Any] = []
        if domain:
            clauses.append("d.domain = %s")
            params.append(domain.strip().lower())
        if status:
            clauses.append("d.status = %s")
            params.append(status.strip().lower())
        if q:
            clauses.append("(d.title ILIKE %s OR d.relative_path ILIKE %s OR d.doc_key ILIKE %s)")
            like = f"%{q}%"
            params.extend([like, like, like])
        where = " AND ".join(clauses)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM documents d WHERE {where}", params)
            total = cur.fetchone()["total"]
            cur.execute(
                f"""
                SELECT d.*, l.code AS library_code, l.name AS library_name,
                       l.retrieval_mode AS library_retrieval_mode,
                       l.embedding_model AS library_embedding_model,
                       l.embedding_model_name AS library_embedding_model_name,
                       l.embedding_dim AS library_embedding_dim,
                       l.rerank_model_name AS library_rerank_model_name,
                       l.keyword_backend AS library_keyword_backend,
                       l.qdrant_collection AS library_qdrant_collection,
                       l.opensearch_index AS library_opensearch_index
                FROM documents d
                JOIN doc_libraries l ON l.id = d.library_id
                WHERE {where}
                ORDER BY d.updated_at DESC, d.title ASC
                LIMIT %s OFFSET %s
                """,
                [*params, limit, offset],
            )
            return {"total": total, "limit": limit, "offset": offset, "items": list(cur.fetchall())}

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        self.init_schema()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.*, l.code AS library_code, l.name AS library_name,
                       l.retrieval_mode AS library_retrieval_mode,
                       l.embedding_model AS library_embedding_model,
                       l.embedding_model_name AS library_embedding_model_name,
                       l.embedding_dim AS library_embedding_dim,
                       l.rerank_model_name AS library_rerank_model_name,
                       l.keyword_backend AS library_keyword_backend,
                       l.qdrant_collection AS library_qdrant_collection,
                       l.opensearch_index AS library_opensearch_index
                FROM documents d
                JOIN doc_libraries l ON l.id = d.library_id
                WHERE d.id = %s AND d.deleted_at IS NULL
                """,
                [document_id],
            )
            doc = cur.fetchone()
            if not doc:
                return None
            cur.execute(
                """
                SELECT id, version, content_hash, content_length, title, relative_path,
                       storage_backend, storage_key, storage_path, storage_etag, storage_size,
                       storage_status, change_type, tombstone, created_at
                FROM document_versions
                WHERE document_id = %s
                ORDER BY version DESC
                """,
                [document_id],
            )
            doc["versions"] = list(cur.fetchall())
            return doc

    def get_document_content(self, document_id: str, *, version: int | None = None) -> dict[str, Any] | None:
        self.init_schema()
        with self._connect() as conn, conn.cursor() as cur:
            if version is None:
                cur.execute("SELECT version FROM documents WHERE id = %s AND deleted_at IS NULL", [document_id])
                row = cur.fetchone()
                if not row:
                    return None
                version = row["version"]
            cur.execute(
                """
                SELECT dv.*, d.title AS current_title
                FROM document_versions dv
                JOIN documents d ON d.id = dv.document_id
                WHERE dv.document_id = %s AND dv.version = %s AND d.deleted_at IS NULL
                """,
                [document_id, version],
            )
            row = cur.fetchone()
            if not row:
                return None
        storage = create_storage(
            row.get("storage_backend") or CODING_RAG_STORAGE_BACKEND,
            seaweedfs_filer_url=CODING_RAG_SEAWEEDFS_FILER_URL,
            seaweedfs_public_base_url=CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
            seaweedfs_bucket=CODING_RAG_SEAWEEDFS_BUCKET,
            seaweedfs_key_prefix=CODING_RAG_SEAWEEDFS_KEY_PREFIX,
        )
        path = row.get("storage_path") or ""
        content = storage.read_text(path, storage_key=row.get("storage_key"), encoding="utf-8") if (path or row.get("storage_key")) else ""
        return {"document_id": document_id, "version": version, "title": row.get("title") or row.get("current_title"), "content": content}

    def set_document_enabled(self, document_id: str, enabled: bool) -> dict[str, Any] | None:
        self.init_schema()
        status = "changed" if enabled else "disabled"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET enabled = %s,
                    status = %s,
                    index_required = CASE WHEN %s THEN TRUE ELSE index_required END,
                    updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                RETURNING *
                """,
                [enabled, status, enabled, document_id],
            )
            row = cur.fetchone()
            conn.commit()
            return row

    def update_document_content(
        self,
        document_id: str,
        content_bytes: bytes,
        *,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Update a document with new content.

        Steps:
        1. Load document from PG, verify enabled and not deleted
        2. Compute content hash of new content
        3. If hash matches current version, return 'unchanged'
        4. Upload new file to storage (SeaweedFS or local)
        5. Create new document_versions row (version + 1)
        6. Update documents.version to new version, set index_required=true, status='changed'
        7. Return new version info

        Document versions are immutable — never modifies existing version rows.
        """
        self.init_schema()

        # 1. Load document
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.*, l.code AS library_code
                FROM documents d
                JOIN doc_libraries l ON l.id = d.library_id
                WHERE d.id = %s AND d.deleted_at IS NULL
                """,
                [document_id],
            )
            doc = cur.fetchone()
        if not doc:
            raise FileNotFoundError(f"Document not found: {document_id}")
        if not doc["enabled"]:
            raise ValueError(f"Document is disabled: {document_id}")

        # 2. Compute content hash
        new_hash = hashlib.sha256(content_bytes).hexdigest()

        # 3. Check if unchanged
        if new_hash == doc["content_hash"]:
            return {
                "document_id": document_id,
                "status": "unchanged",
                "version": int(doc["version"]),
                "content_hash": new_hash,
            }

        # 4. Upload to storage
        relative_path = doc["relative_path"] or doc["doc_key"]
        storage = create_storage(
            CODING_RAG_STORAGE_BACKEND,
            seaweedfs_filer_url=CODING_RAG_SEAWEEDFS_FILER_URL,
            seaweedfs_public_base_url=CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
            seaweedfs_bucket=CODING_RAG_SEAWEEDFS_BUCKET,
            seaweedfs_key_prefix=CODING_RAG_SEAWEEDFS_KEY_PREFIX,
        )
        # Write content to a temp file, then upload via storage
        suffix = Path(relative_path).suffix or ".md"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content_bytes)
            tmp_path = Path(tmp.name)
        try:
            stored = storage.put_existing_file(tmp_path, relative_path=relative_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        # 5 & 6. Create new version row and update document
        new_version = int(doc["version"]) + 1
        new_length = len(content_bytes)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO document_versions (
                    id, document_id, version, content_hash, content_length,
                    title, source_url, source_file, relative_path,
                    storage_path, storage_backend, storage_bucket, storage_key,
                    storage_etag, storage_size, storage_status, change_type, tombstone
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, 'update', FALSE
                )
                """,
                [
                    str(uuid.uuid4()), document_id, new_version, new_hash, new_length,
                    doc["title"], doc.get("source_url"), doc.get("source_file"), relative_path,
                    stored.storage_path, stored.storage_backend, stored.storage_bucket, stored.storage_key,
                    stored.storage_etag, stored.storage_size, stored.storage_status,
                ],
            )
            cur.execute(
                """
                UPDATE documents
                SET version = %s,
                    content_hash = %s,
                    content_length = %s,
                    status = 'changed',
                    index_required = TRUE,
                    error_message = NULL,
                    updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                """,
                [new_version, new_hash, new_length, document_id],
            )
            conn.commit()

        # 7. Return result
        return {
            "document_id": document_id,
            "status": "changed",
            "version": new_version,
            "content_hash": new_hash,
            "content_length": new_length,
            "storage_backend": stored.storage_backend,
            "storage_status": stored.storage_status,
        }

    def retention_preview(self, library_id: str, *, keep: int = DEFAULT_RETENTION_VERSIONS) -> dict[str, Any]:
        self.init_schema()
        keep = max(1, keep)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, code, name FROM doc_libraries WHERE id = %s", [library_id])
            library = cur.fetchone()
            if not library:
                raise KeyError(library_id)
            cur.execute(
                """
                WITH ranked AS (
                  SELECT dv.*, d.title AS doc_title, d.relative_path AS doc_relative_path,
                         row_number() OVER (PARTITION BY dv.document_id ORDER BY dv.version DESC) AS rn
                  FROM document_versions dv
                  JOIN documents d ON d.id = dv.document_id
                  WHERE d.library_id = %s AND dv.storage_status = 'active'
                )
                SELECT document_id, version, doc_title AS title, doc_relative_path AS relative_path, storage_backend, storage_key, storage_size
                FROM ranked
                WHERE rn > %s
                ORDER BY relative_path, version DESC
                """,
                [library_id, keep],
            )
            candidates = list(cur.fetchall())
            summary = {
                "library": library,
                "retention_versions": keep,
                "candidate_count": len(candidates),
                "candidate_storage_size": sum((c.get("storage_size") or 0) for c in candidates),
                "candidates": candidates,
                "dry_run": True,
            }
            cur.execute(
                """
                INSERT INTO document_retention_jobs (id, library_id, status, retention_versions, dry_run, summary, started_at, finished_at)
                VALUES (%s, %s, 'completed', %s, TRUE, %s::jsonb, now(), now())
                RETURNING id
                """,
                [str(uuid.uuid4()), library_id, keep, Jsonb(to_jsonable(summary))],
            )
            job_id = cur.fetchone()["id"]
            conn.commit()
            summary["job_id"] = job_id
            return summary

    def export_library(self, library_id: str, *, archive_format: str = "tar.gz", output_dir: str | None = None) -> dict[str, Any]:
        self.init_schema()
        archive_format = (archive_format or "tar.gz").strip().lower()
        if archive_format not in {"tar.gz", "tgz", "zip"}:
            raise ValueError("archive format must be tar.gz or zip")
        archive_format = "tar.gz" if archive_format == "tgz" else archive_format
        out_dir = Path(output_dir or "output/library-transfers").expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)

        job_id = str(uuid.uuid4())
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM doc_libraries WHERE id = %s", [library_id])
            library = cur.fetchone()
            if not library:
                raise KeyError(library_id)
            cur.execute("INSERT INTO library_transfer_jobs (id, library_id, direction, status, dry_run, started_at) VALUES (%s, %s, 'export', 'running', FALSE, now())", [job_id, library_id])
            conn.commit()

        try:
            with tempfile.TemporaryDirectory(prefix="codingrag-export-") as tmp_name:
                tmp = Path(tmp_name)
                (tmp / "data").mkdir()
                (tmp / "files").mkdir()
                with self._connect() as conn, conn.cursor() as cur:
                    cur.execute("SELECT * FROM doc_libraries WHERE id = %s", [library_id])
                    library = cur.fetchone()
                    cur.execute("SELECT * FROM documents WHERE library_id = %s AND deleted_at IS NULL ORDER BY relative_path, doc_key", [library_id])
                    documents = list(cur.fetchall())
                    cur.execute(
                        """
                        SELECT dv.*, d.doc_key
                        FROM document_versions dv
                        JOIN documents d ON d.id = dv.document_id
                        WHERE d.library_id = %s AND d.deleted_at IS NULL
                        ORDER BY d.relative_path, dv.version
                        """,
                        [library_id],
                    )
                    versions = list(cur.fetchall())

                storage_cache: dict[str, Any] = {}
                checksums: list[str] = []
                version_exports: list[dict[str, Any]] = []
                for version in versions:
                    rel = _export_file_path(version)
                    content = self._read_version_bytes(version, storage_cache)
                    target = tmp / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    checksums.append(f"sha256:{hashlib.sha256(content).hexdigest()}  {rel}")
                    row = dict(version)
                    row["archive_file"] = rel
                    version_exports.append(row)

                manifest = {
                    "format": "codingrag-library-transfer/v1",
                    "archive_kind": archive_format,
                    "exported_at": _now_iso(),
                    "includes": ["manifest", "metadata", "original_files", "checksums"],
                    "excludes": ["qdrant_snapshots", "opensearch_snapshots"],
                    "library": to_jsonable(library),
                    "counts": {"documents": len(documents), "document_versions": len(versions), "files": len(version_exports)},
                }
                (tmp / "manifest.json").write_text(json.dumps(to_jsonable(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
                _write_jsonl(tmp / "data" / "doc_libraries.jsonl", [library])
                _write_jsonl(tmp / "data" / "documents.jsonl", documents)
                _write_jsonl(tmp / "data" / "document_versions.jsonl", version_exports)
                (tmp / "checksums.txt").write_text("\n".join(checksums) + ("\n" if checksums else ""), encoding="utf-8")

                suffix = ".tar.gz" if archive_format == "tar.gz" else ".zip"
                archive_name = f"codingrag-{_safe_slug(library['code'])}-{job_id[:8]}{suffix}"
                archive_path = out_dir / archive_name
                if archive_format == "zip":
                    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                        for path in sorted(tmp.rglob("*")):
                            if path.is_file():
                                zf.write(path, path.relative_to(tmp).as_posix())
                else:
                    with tarfile.open(archive_path, "w:gz") as tf:
                        for path in sorted(tmp.rglob("*")):
                            tf.add(path, arcname=path.relative_to(tmp).as_posix(), recursive=False)

            summary = {"library": {"id": library_id, "code": library["code"], "name": library["name"]}, "archive_path": str(archive_path), "counts": manifest["counts"]}
            self._finish_transfer_job(job_id, "completed", archive_path=str(archive_path), summary=summary)
            summary["job_id"] = job_id
            return summary
        except Exception as exc:
            self._finish_transfer_job(job_id, "failed", error_message=str(exc))
            raise

    def preview_library_import(self, archive_path: str, *, mode: str | None = None, new_library_code: str | None = None) -> dict[str, Any]:
        try:
            return self._import_library_archive(archive_path, mode=mode, new_library_code=new_library_code, dry_run=True)
        except Exception as exc:
            self._record_failed_transfer_job(archive_path, mode=mode, dry_run=True, error_message=str(exc))
            raise

    def import_library(self, archive_path: str, *, mode: str | None = None, new_library_code: str | None = None) -> dict[str, Any]:
        try:
            return self._import_library_archive(archive_path, mode=mode, new_library_code=new_library_code, dry_run=False)
        except Exception as exc:
            self._record_failed_transfer_job(archive_path, mode=mode, dry_run=False, error_message=str(exc))
            raise

    def enqueue_library_import(self, archive_path: str, *, mode: str | None = None, new_library_code: str | None = None) -> dict[str, Any]:
        """Create a pending import job and return immediately.

        The API uses this path by default so large archives are handled by an
        explicit worker process instead of tying work to the HTTP request.
        """
        self.init_schema()
        normalized_mode = self._normalize_import_mode(mode)
        archive = Path(archive_path).expanduser().resolve()
        if not archive.is_file():
            raise FileNotFoundError(str(archive))
        if normalized_mode == "rename-library" and not new_library_code:
            raise ValueError("new_library_code is required for rename-library")
        target_hint = new_library_code.strip().lower() if new_library_code else None
        if target_hint and not re.fullmatch(r"[0-9a-z][0-9a-z._-]{0,120}", target_hint):
            raise ValueError("new_library_code/library code contains unsafe characters")

        job_id = str(uuid.uuid4())
        summary = {
            "async": True,
            "dry_run": False,
            "mode": normalized_mode,
            "archive_path": str(archive),
            "new_library_code": target_hint,
            "batch_size": CODING_RAG_IMPORT_BATCH_SIZE,
            "total_documents": 0,
            "processed": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "conflict": 0,
            "failed": 0,
            "current_doc_key": None,
            "errors": [],
        }
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO library_transfer_jobs (id, direction, archive_path, status, mode, dry_run, summary)
                VALUES (%s, 'import', %s, 'pending', %s, FALSE, %s::jsonb)
                """,
                [job_id, str(archive), normalized_mode, Jsonb(to_jsonable(summary))],
            )
            conn.commit()
        summary["job_id"] = job_id
        summary["status"] = "pending"
        return summary

    def get_transfer_job(self, job_id: str) -> dict[str, Any] | None:
        self.init_schema()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM library_transfer_jobs WHERE id = %s", [job_id])
            return cur.fetchone()

    def _normalize_import_mode(self, mode: str | None) -> str:
        normalized_mode = (mode or "skip").strip().lower()
        if normalized_mode not in {"skip", "upsert", "replace-library", "rename-library"}:
            raise ValueError("mode must be skip, upsert, replace-library, or rename-library")
        return normalized_mode

    def _import_library_archive(self, archive_path: str, *, mode: str | None, new_library_code: str | None, dry_run: bool) -> dict[str, Any]:
        self.init_schema()
        normalized_mode = self._normalize_import_mode(mode)
        job_id = str(uuid.uuid4())
        archive = Path(archive_path).expanduser().resolve()
        if not archive.is_file():
            raise FileNotFoundError(str(archive))

        with tempfile.TemporaryDirectory(prefix="codingrag-import-") as tmp_name:
            tmp = Path(tmp_name)
            _safe_extract_archive(archive, tmp)
            manifest_path = tmp / "manifest.json"
            if not manifest_path.is_file():
                raise ValueError("archive missing manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "codingrag-library-transfer/v1":
                raise ValueError("unsupported archive manifest format")
            libraries = _read_jsonl(tmp / "data" / "doc_libraries.jsonl")
            documents = _read_jsonl(tmp / "data" / "documents.jsonl")
            versions = _read_jsonl(tmp / "data" / "document_versions.jsonl")
            if len(libraries) != 1:
                raise ValueError("archive must contain exactly one library")
            source_library = libraries[0]
            target_code = (new_library_code or source_library["code"]).strip().lower()
            if normalized_mode == "rename-library" and not new_library_code:
                raise ValueError("new_library_code is required for rename-library")
            if not re.fullmatch(r"[0-9a-z][0-9a-z._-]{0,120}", target_code):
                raise ValueError("new_library_code/library code contains unsafe characters")

            stats = {"created": 0, "updated": 0, "skipped": 0, "conflict": 0}
            planned: list[dict[str, Any]] = []
            doc_by_old_id = {d["id"]: d for d in documents}
            versions_by_doc: dict[str, list[dict[str, Any]]] = {}
            for version in versions:
                rel = _validate_archive_member(version.get("archive_file") or "")
                if not rel.startswith("files/") or not (tmp / rel).is_file():
                    raise ValueError(f"missing exported file for version: {rel}")
                versions_by_doc.setdefault(version["document_id"], []).append(version)

            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM doc_libraries WHERE code = %s", [target_code])
                existing_library = cur.fetchone()
                if existing_library and not dry_run and not mode:
                    raise ValueError("target library code already exists; use preview or explicit import mode")
                if existing_library and normalized_mode == "replace-library":
                    planned.append({"action": "archive-existing-library", "library_id": existing_library["id"], "code": target_code})
                elif existing_library and normalized_mode == "rename-library":
                    raise ValueError(f"target library code already exists: {target_code}")

                target_library_id = existing_library["id"] if existing_library and normalized_mode != "replace-library" else str(uuid.uuid4())
                for doc in documents:
                    doc_key = _retarget_doc_key(doc["doc_key"], source_library["code"], target_code)
                    latest_hash = doc["content_hash"]
                    cur.execute("SELECT * FROM documents WHERE library_id = %s AND doc_key = %s AND deleted_at IS NULL", [target_library_id, doc_key])
                    existing_doc = cur.fetchone()
                    if not existing_doc:
                        stats["created"] += 1
                        planned.append({"action": "create", "doc_key": doc_key})
                    elif existing_doc["content_hash"] == latest_hash:
                        stats["skipped"] += 1
                        planned.append({"action": "skip", "doc_key": doc_key})
                    elif normalized_mode == "upsert":
                        stats["updated"] += 1
                        planned.append({"action": "update", "doc_key": doc_key})
                    elif normalized_mode == "replace-library":
                        stats["created"] += 1
                        planned.append({"action": "replace-create", "doc_key": doc_key})
                    else:
                        stats["conflict"] += 1
                        planned.append({"action": "conflict", "doc_key": doc_key})

                summary = {
                    "dry_run": dry_run,
                    "mode": normalized_mode,
                    "archive_path": str(archive),
                    "source_library": {"id": source_library.get("id"), "code": source_library.get("code"), "name": source_library.get("name")},
                    "target_library_code": target_code,
                    "stats": stats,
                    "planned": planned[:200],
                }
                cur.execute(
                    "INSERT INTO library_transfer_jobs (id, library_id, direction, archive_path, status, mode, dry_run, summary, started_at) VALUES (%s, %s, 'import', %s, 'running', %s, %s, %s::jsonb, now())",
                    [job_id, existing_library["id"] if existing_library else None, str(archive), normalized_mode, dry_run, Jsonb(to_jsonable(summary))],
                )
                if dry_run:
                    cur.execute("UPDATE library_transfer_jobs SET status = 'completed', finished_at = now() WHERE id = %s", [job_id])
                    conn.commit()
                    summary["job_id"] = job_id
                    return summary
                if stats["conflict"] and normalized_mode == "skip":
                    raise ValueError("import has conflicts in skip mode; run preview or use upsert/rename-library")

                if existing_library and normalized_mode == "replace-library":
                    archived_code = f"{target_code}-archived-{job_id[:8]}"
                    cur.execute("UPDATE doc_libraries SET code = %s, enabled = FALSE, metadata = metadata || %s::jsonb, updated_at = now() WHERE id = %s", [archived_code, Jsonb({"archived_by_transfer_job": job_id, "archived_from_code": target_code}), existing_library["id"]])
                    existing_library = None
                if not existing_library or normalized_mode == "replace-library":
                    target_library_id = str(uuid.uuid4())
                    self._insert_imported_library(cur, target_library_id, source_library, target_code)
                else:
                    target_library_id = existing_library["id"]

                storage = create_storage(
                    CODING_RAG_STORAGE_BACKEND,
                    seaweedfs_filer_url=CODING_RAG_SEAWEEDFS_FILER_URL,
                    seaweedfs_public_base_url=CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
                    seaweedfs_bucket=CODING_RAG_SEAWEEDFS_BUCKET,
                    seaweedfs_key_prefix=CODING_RAG_SEAWEEDFS_KEY_PREFIX,
                )
                old_to_new_doc_id: dict[str, str] = {}
                for doc in documents:
                    doc_key = _retarget_doc_key(doc["doc_key"], source_library["code"], target_code)
                    doc_versions = sorted(versions_by_doc.get(doc["id"], []), key=lambda v: int(v["version"]))
                    if not doc_versions:
                        continue
                    latest_file = tmp / _validate_archive_member(doc_versions[-1]["archive_file"])
                    stored = storage.put_existing_file(latest_file, relative_path=doc.get("relative_path") or doc_key)
                    cur.execute("SELECT * FROM documents WHERE library_id = %s AND doc_key = %s AND deleted_at IS NULL", [target_library_id, doc_key])
                    existing_doc = cur.fetchone()
                    if existing_doc and existing_doc["content_hash"] == doc["content_hash"]:
                        old_to_new_doc_id[doc["id"]] = existing_doc["id"]
                        continue
                    if existing_doc and normalized_mode != "upsert":
                        continue
                    if existing_doc:
                        new_doc_id = existing_doc["id"]
                        old_to_new_doc_id[doc["id"]] = new_doc_id
                        new_version = int(existing_doc["version"]) + 1
                        self._update_imported_document(cur, new_doc_id, doc, doc_key, target_library_id, target_code, new_version, stored)
                        self._insert_imported_version(cur, new_doc_id, doc_versions[-1], new_version, stored)
                    else:
                        new_doc_id = str(uuid.uuid4())
                        old_to_new_doc_id[doc["id"]] = new_doc_id
                        self._insert_imported_document(cur, new_doc_id, doc, doc_key, target_library_id, target_code, stored)
                        for version in doc_versions:
                            version_file = tmp / _validate_archive_member(version["archive_file"])
                            version_stored = stored if version is doc_versions[-1] else storage.put_existing_file(version_file, relative_path=version.get("relative_path") or doc.get("relative_path") or doc_key)
                            self._insert_imported_version(cur, new_doc_id, version, int(version["version"]), version_stored)
                summary["target_library_id"] = target_library_id
                cur.execute("UPDATE library_transfer_jobs SET library_id = %s, status = 'completed', summary = %s::jsonb, finished_at = now() WHERE id = %s", [target_library_id, Jsonb(to_jsonable(summary)), job_id])
                conn.commit()
                summary["job_id"] = job_id
                return summary

    def run_pending_import_jobs(self, *, limit: int = 1, batch_size: int | None = None) -> list[dict[str, Any]]:
        """Run pending async import jobs serially. Intended for a CLI worker."""
        self.init_schema()
        results: list[dict[str, Any]] = []
        limit = max(1, limit)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM library_transfer_jobs
                WHERE direction = 'import' AND status = 'pending' AND dry_run = FALSE
                ORDER BY created_at ASC
                LIMIT %s
                """,
                [limit],
            )
            job_ids = [str(row["id"]) for row in cur.fetchall()]
        for job_id in job_ids:
            results.append(self.run_import_job(job_id, batch_size=batch_size))
        return results

    def run_import_job(self, job_id: str, *, batch_size: int | None = None) -> dict[str, Any]:
        self.init_schema()
        batch_size = max(1, int(batch_size or CODING_RAG_IMPORT_BATCH_SIZE or 100))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE library_transfer_jobs
                SET status = 'running', started_at = COALESCE(started_at, now()), error_message = NULL
                WHERE id = %s AND direction = 'import' AND dry_run = FALSE AND status IN ('pending', 'failed')
                RETURNING *
                """,
                [job_id],
            )
            job = cur.fetchone()
            if not job:
                cur.execute("SELECT * FROM library_transfer_jobs WHERE id = %s", [job_id])
                existing = cur.fetchone()
                if not existing:
                    raise KeyError(job_id)
                return existing
            conn.commit()

        try:
            summary = self._run_import_job_archive(job, batch_size=batch_size)
            return summary
        except Exception as exc:
            current = self.get_transfer_job(job_id) or {}
            summary = dict(current.get("summary") or {})
            summary.setdefault("errors", []).append({"error": str(exc)[:1000]})
            self._finish_transfer_job(job_id, "failed", summary=summary, error_message=str(exc)[:2000])
            raise

    def _run_import_job_archive(self, job: dict[str, Any], *, batch_size: int) -> dict[str, Any]:
        job_id = str(job["id"])
        normalized_mode = self._normalize_import_mode(job.get("mode"))
        archive = Path(job["archive_path"]).expanduser().resolve()
        if not archive.is_file():
            raise FileNotFoundError(str(archive))
        initial_summary = dict(job.get("summary") or {})
        new_library_code = initial_summary.get("new_library_code")

        with tempfile.TemporaryDirectory(prefix="codingrag-import-job-") as tmp_name:
            tmp = Path(tmp_name)
            _safe_extract_archive(archive, tmp)
            manifest_path = tmp / "manifest.json"
            if not manifest_path.is_file():
                raise ValueError("archive missing manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "codingrag-library-transfer/v1":
                raise ValueError("unsupported archive manifest format")
            libraries = _read_jsonl(tmp / "data" / "doc_libraries.jsonl")
            documents = _read_jsonl(tmp / "data" / "documents.jsonl")
            versions = _read_jsonl(tmp / "data" / "document_versions.jsonl")
            if len(libraries) != 1:
                raise ValueError("archive must contain exactly one library")
            source_library = libraries[0]
            target_code = (new_library_code or source_library["code"]).strip().lower()
            if normalized_mode == "rename-library" and not new_library_code:
                raise ValueError("new_library_code is required for rename-library")
            if not re.fullmatch(r"[0-9a-z][0-9a-z._-]{0,120}", target_code):
                raise ValueError("new_library_code/library code contains unsafe characters")

            versions_by_doc: dict[str, list[dict[str, Any]]] = {}
            for version in versions:
                rel = _validate_archive_member(version.get("archive_file") or "")
                if not rel.startswith("files/") or not (tmp / rel).is_file():
                    raise ValueError(f"missing exported file for version: {rel}")
                versions_by_doc.setdefault(version["document_id"], []).append(version)

            summary = {
                **initial_summary,
                "async": True,
                "dry_run": False,
                "mode": normalized_mode,
                "archive_path": str(archive),
                "source_library": {"id": source_library.get("id"), "code": source_library.get("code"), "name": source_library.get("name")},
                "target_library_code": target_code,
                "batch_size": batch_size,
                "total_documents": len(documents),
                "processed": 0,
                "created": 0,
                "updated": 0,
                "skipped": 0,
                "conflict": 0,
                "failed": 0,
                "current_doc_key": None,
                "errors": [],
            }

            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT * FROM doc_libraries WHERE code = %s", [target_code])
                existing_library = cur.fetchone()
                expected_library_id = str(job.get("library_id") or initial_summary.get("target_library_id") or "")
                if existing_library and normalized_mode == "rename-library" and str(existing_library["id"]) != expected_library_id:
                    raise ValueError(f"target library code already exists: {target_code}")
                if existing_library and normalized_mode == "replace-library" and str(existing_library["id"]) != expected_library_id:
                    archived_code = f"{target_code}-archived-{job_id[:8]}"
                    cur.execute(
                        "UPDATE doc_libraries SET code = %s, enabled = FALSE, metadata = metadata || %s::jsonb, updated_at = now() WHERE id = %s",
                        [archived_code, Jsonb({"archived_by_transfer_job": job_id, "archived_from_code": target_code}), existing_library["id"]],
                    )
                    existing_library = None
                if existing_library:
                    target_library_id = existing_library["id"]
                else:
                    target_library_id = str(uuid.uuid4())
                    self._insert_imported_library(cur, target_library_id, source_library, target_code)
                summary["target_library_id"] = target_library_id
                cur.execute("UPDATE library_transfer_jobs SET library_id = %s, summary = %s::jsonb WHERE id = %s", [target_library_id, Jsonb(to_jsonable(summary)), job_id])
                conn.commit()

            storage = create_storage(
                CODING_RAG_STORAGE_BACKEND,
                seaweedfs_filer_url=CODING_RAG_SEAWEEDFS_FILER_URL,
                seaweedfs_public_base_url=CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
                seaweedfs_bucket=CODING_RAG_SEAWEEDFS_BUCKET,
                seaweedfs_key_prefix=CODING_RAG_SEAWEEDFS_KEY_PREFIX,
            )

            for start in range(0, len(documents), batch_size):
                batch = documents[start : start + batch_size]
                with self._connect() as conn:
                    with conn.transaction():
                        for doc in batch:
                            doc_key = _retarget_doc_key(doc["doc_key"], source_library["code"], target_code)
                            summary["current_doc_key"] = doc_key
                            try:
                                with conn.transaction():
                                    with conn.cursor() as cur:
                                        action = self._import_one_document(cur, storage, tmp, versions_by_doc, doc, doc_key, summary["target_library_id"], target_code, normalized_mode)
                                        summary[action] += 1
                            except Exception as exc:
                                summary["failed"] += 1
                                summary["errors"].append({"doc_key": doc_key, "error": str(exc)[:1000]})
                                summary["errors"] = summary["errors"][-50:]
                            finally:
                                summary["processed"] += 1
                    with conn.cursor() as cur:
                        cur.execute("UPDATE library_transfer_jobs SET summary = %s::jsonb WHERE id = %s", [Jsonb(to_jsonable(summary)), job_id])
                        conn.commit()

            summary["current_doc_key"] = None
            status = "completed" if summary["failed"] == 0 else "failed"
            self._finish_transfer_job(job_id, status, summary=summary, error_message=None if status == "completed" else "one or more documents failed")
            summary["job_id"] = job_id
            summary["status"] = status
            return summary

    def _import_one_document(self, cur, storage, tmp: Path, versions_by_doc: dict[str, list[dict[str, Any]]], doc: dict[str, Any], doc_key: str, target_library_id: str, target_code: str, normalized_mode: str) -> str:
        doc_versions = sorted(versions_by_doc.get(doc["id"], []), key=lambda v: int(v["version"]))
        if not doc_versions:
            return "skipped"
        cur.execute("SELECT * FROM documents WHERE library_id = %s AND doc_key = %s AND deleted_at IS NULL", [target_library_id, doc_key])
        existing_doc = cur.fetchone()
        if existing_doc and existing_doc["content_hash"] == doc["content_hash"] and int(existing_doc["version"]) >= int(doc.get("version") or 1):
            return "skipped"
        if existing_doc and normalized_mode == "skip":
            return "conflict"
        if existing_doc and normalized_mode not in {"upsert", "replace-library"}:
            return "conflict"

        latest_file = tmp / _validate_archive_member(doc_versions[-1]["archive_file"])
        stored = storage.put_existing_file(latest_file, relative_path=doc.get("relative_path") or doc_key)
        if existing_doc:
            new_doc_id = existing_doc["id"]
            new_version = int(existing_doc["version"]) + 1
            self._update_imported_document(cur, new_doc_id, doc, doc_key, target_library_id, target_code, new_version, stored)
            self._insert_imported_version(cur, new_doc_id, doc_versions[-1], new_version, stored)
            return "updated"

        new_doc_id = str(uuid.uuid4())
        self._insert_imported_document(cur, new_doc_id, doc, doc_key, target_library_id, target_code, stored)
        for version in doc_versions:
            version_file = tmp / _validate_archive_member(version["archive_file"])
            version_stored = stored if version is doc_versions[-1] else storage.put_existing_file(version_file, relative_path=version.get("relative_path") or doc.get("relative_path") or doc_key)
            self._insert_imported_version(cur, new_doc_id, version, int(version["version"]), version_stored)
        return "created"

    def _read_version_bytes(self, version: dict[str, Any], storage_cache: dict[str, Any]) -> bytes:
        backend = version.get("storage_backend") or CODING_RAG_STORAGE_BACKEND
        if backend not in storage_cache:
            storage_cache[backend] = create_storage(
                backend,
                seaweedfs_filer_url=CODING_RAG_SEAWEEDFS_FILER_URL,
                seaweedfs_public_base_url=CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
                seaweedfs_bucket=CODING_RAG_SEAWEEDFS_BUCKET,
                seaweedfs_key_prefix=CODING_RAG_SEAWEEDFS_KEY_PREFIX,
            )
        text = storage_cache[backend].read_text(version.get("storage_path") or "", storage_key=version.get("storage_key"), encoding="utf-8")
        return text.encode("utf-8")

    def _insert_imported_library(self, cur, library_id: str, source: dict[str, Any], target_code: str) -> None:
        cur.execute(
            """
            INSERT INTO doc_libraries (
              id, code, name, description, domain, source_type, source_uri, root_path, enabled, version,
              retrieval_mode, embedding_model, embedding_model_name, embedding_dim, rerank_model_name,
              keyword_backend, qdrant_collection, opensearch_index, metadata
            ) VALUES (%s, %s, %s, %s, %s, 'archive', %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            [
                library_id, target_code, source.get("name") or target_code, source.get("description"), target_code,
                source.get("source_uri"), source.get("enabled", True), source.get("version") or "1.0.0",
                source.get("retrieval_mode") or "hybrid_rerank", source.get("embedding_model"), source.get("embedding_model_name"),
                source.get("embedding_dim"), source.get("rerank_model_name"), source.get("keyword_backend"),
                source.get("qdrant_collection"), source.get("opensearch_index"), Jsonb(source.get("metadata") or {}),
            ],
        )

    def _insert_imported_document(self, cur, doc_id: str, source: dict[str, Any], doc_key: str, library_id: str, target_code: str, stored) -> None:
        cur.execute(
            """
            INSERT INTO documents (
              id, library_id, domain, doc_key, title, source_url, source_file, local_path, relative_path,
              mime_type, language, content_hash, content_length, version, enabled, status, index_required,
              chunk_count, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s::jsonb)
            """,
            [
                doc_id, library_id, target_code, doc_key, source.get("title") or doc_key, source.get("source_url"),
                source.get("source_file"), stored.storage_path or "", source.get("relative_path") or doc_key,
                source.get("mime_type"), source.get("language"), source.get("content_hash"), source.get("content_length") or 0,
                source.get("version") or 1, source.get("enabled", True), source.get("status") or "changed",
                source.get("chunk_count") or 0, Jsonb(source.get("metadata") or {}),
            ],
        )

    def _update_imported_document(self, cur, doc_id: str, source: dict[str, Any], doc_key: str, library_id: str, target_code: str, new_version: int, stored) -> None:
        cur.execute(
            """
            UPDATE documents SET
              title = %s, source_url = %s, source_file = %s, local_path = %s, relative_path = %s,
              mime_type = %s, language = %s, content_hash = %s, content_length = %s, version = %s,
              enabled = %s, status = 'changed', index_required = TRUE, error_message = NULL, updated_at = now(), metadata = %s::jsonb
            WHERE id = %s
            """,
            [
                source.get("title") or doc_key, source.get("source_url"), source.get("source_file"), stored.storage_path or "",
                source.get("relative_path") or doc_key, source.get("mime_type"), source.get("language"), source.get("content_hash"),
                source.get("content_length") or 0, new_version, source.get("enabled", True), Jsonb(source.get("metadata") or {}), doc_id,
            ],
        )

    def _insert_imported_version(self, cur, doc_id: str, source: dict[str, Any], version: int, stored) -> None:
        cur.execute(
            """
            INSERT INTO document_versions (
              id, document_id, version, content_hash, content_length, title, source_url, source_file, relative_path,
              storage_path, storage_backend, storage_bucket, storage_key, storage_etag, storage_size,
              storage_status, change_type, tombstone, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (document_id, version) DO NOTHING
            """,
            [
                str(uuid.uuid4()), doc_id, version, source.get("content_hash"), source.get("content_length") or 0,
                source.get("title"), source.get("source_url"), source.get("source_file"), source.get("relative_path") or "",
                stored.storage_path, stored.storage_backend, stored.storage_bucket, stored.storage_key, stored.storage_etag,
                stored.storage_size, stored.storage_status, source.get("change_type") or "update", source.get("tombstone", False),
                Jsonb(source.get("metadata") or {}),
            ],
        )

    def _record_failed_transfer_job(self, archive_path: str, *, mode: str | None, dry_run: bool, error_message: str) -> None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO library_transfer_jobs (id, direction, archive_path, status, mode, dry_run, summary, error_message, started_at, finished_at) VALUES (%s, 'import', %s, 'failed', %s, %s, '{}'::jsonb, %s, now(), now())",
                    [str(uuid.uuid4()), archive_path, (mode or "skip").strip().lower(), dry_run, error_message[:2000]],
                )
                conn.commit()
        except Exception:
            logger.exception("failed to record failed import transfer job")

    def _finish_transfer_job(self, job_id: str, status: str, *, archive_path: str | None = None, summary: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE library_transfer_jobs SET status = %s, archive_path = COALESCE(%s, archive_path), summary = COALESCE(%s::jsonb, summary), error_message = %s, finished_at = now() WHERE id = %s",
                [status, archive_path, Jsonb(to_jsonable(summary)) if summary is not None else None, error_message, job_id],
            )
            conn.commit()

    def _upsert_library(self, cur, domain: str, cfg: dict[str, Any], docs_dir: Path) -> str:
        library_id = str(uuid.uuid4())
        name = cfg.get("display_name") or domain
        search_cfg = build_library_search_config(domain, cfg)
        metadata = {"collection": cfg.get("collection"), "language": cfg.get("language")}
        cur.execute(
            """
            INSERT INTO doc_libraries (
              id, code, name, domain, source_uri, root_path,
              retrieval_mode, embedding_model, embedding_model_name, embedding_dim,
              rerank_model_name, keyword_backend, qdrant_collection, opensearch_index,
              metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (code) DO UPDATE SET
              name = EXCLUDED.name,
              domain = EXCLUDED.domain,
              source_uri = EXCLUDED.source_uri,
              root_path = EXCLUDED.root_path,
              retrieval_mode = EXCLUDED.retrieval_mode,
              embedding_model = EXCLUDED.embedding_model,
              embedding_model_name = EXCLUDED.embedding_model_name,
              embedding_dim = EXCLUDED.embedding_dim,
              rerank_model_name = EXCLUDED.rerank_model_name,
              keyword_backend = EXCLUDED.keyword_backend,
              qdrant_collection = EXCLUDED.qdrant_collection,
              opensearch_index = EXCLUDED.opensearch_index,
              metadata = doc_libraries.metadata || EXCLUDED.metadata,
              updated_at = now()
            RETURNING id
            """,
            [
                library_id,
                domain,
                name,
                domain,
                str(docs_dir),
                str(docs_dir),
                search_cfg["retrieval_mode"],
                search_cfg["embedding_model"],
                search_cfg["embedding_model_name"],
                search_cfg["embedding_dim"],
                search_cfg["rerank_model_name"],
                search_cfg["keyword_backend"],
                search_cfg["qdrant_collection"],
                search_cfg["opensearch_index"],
                Jsonb(metadata),
            ],
        )
        return cur.fetchone()["id"]

    def _upsert_document(self, cur, library_id: str, metadata: dict[str, Any], stored, scan_run_id: str) -> str:
        cur.execute(
            "SELECT * FROM documents WHERE library_id = %s AND doc_key = %s",
            [library_id, metadata["doc_key"]],
        )
        existing = cur.fetchone()
        if not existing:
            doc_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO documents (
                  id, library_id, domain, doc_key, title, source_file, local_path, relative_path,
                  mime_type, language, content_hash, content_length, version, status,
                  last_scanned_at, scan_run_id, index_required, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 'new', now(), %s, TRUE, %s::jsonb)
                """,
                [
                    doc_id,
                    library_id,
                    metadata["domain"],
                    metadata["doc_key"],
                    metadata["title"],
                    metadata["source_file"],
                    metadata["local_path"],
                    metadata["relative_path"],
                    metadata["mime_type"],
                    metadata["language"],
                    metadata["content_hash"],
                    metadata["content_length"],
                    scan_run_id,
                    Jsonb(metadata.get("metadata", {})),
                ],
            )
            self._insert_version(cur, doc_id, 1, metadata, stored, "create")
            return "created"

        doc_id = existing["id"]
        if existing["content_hash"] == metadata["content_hash"]:
            cur.execute(
                """
                UPDATE documents SET
                  title = %s, source_file = %s, local_path = %s, relative_path = %s,
                  mime_type = %s, content_length = %s, last_scanned_at = now(), scan_run_id = %s,
                  updated_at = now()
                WHERE id = %s
                """,
                [
                    metadata["title"],
                    metadata["source_file"],
                    metadata["local_path"],
                    metadata["relative_path"],
                    metadata["mime_type"],
                    metadata["content_length"],
                    scan_run_id,
                    doc_id,
                ],
            )
            self._update_version_storage(cur, doc_id, int(existing["version"]), stored)
            return "unchanged"

        new_version = int(existing["version"]) + 1
        cur.execute(
            """
            UPDATE documents SET
              title = %s, source_file = %s, local_path = %s, relative_path = %s,
              mime_type = %s, language = %s, content_hash = %s, content_length = %s,
              version = %s, status = 'changed', last_scanned_at = now(), scan_run_id = %s,
              index_required = TRUE, error_message = NULL, updated_at = now()
            WHERE id = %s
            """,
            [
                metadata["title"],
                metadata["source_file"],
                metadata["local_path"],
                metadata["relative_path"],
                metadata["mime_type"],
                metadata["language"],
                metadata["content_hash"],
                metadata["content_length"],
                new_version,
                scan_run_id,
                doc_id,
            ],
        )
        self._insert_version(cur, doc_id, new_version, metadata, stored, "update")
        return "changed"

    def _update_version_storage(self, cur, doc_id: str, version: int, stored) -> None:
        cur.execute(
            """
            UPDATE document_versions
            SET storage_path = %s,
                storage_backend = %s,
                storage_bucket = %s,
                storage_key = %s,
                storage_etag = %s,
                storage_size = %s,
                storage_status = %s
            WHERE document_id = %s AND version = %s
            """,
            [
                stored.storage_path,
                stored.storage_backend,
                stored.storage_bucket,
                stored.storage_key,
                stored.storage_etag,
                stored.storage_size,
                stored.storage_status,
                doc_id,
                version,
            ],
        )

    def _insert_version(self, cur, doc_id: str, version: int, metadata: dict[str, Any], stored, change_type: str) -> None:
        cur.execute(
            """
            INSERT INTO document_versions (
              id, document_id, version, content_hash, content_length, title, source_file, relative_path,
              storage_path, storage_backend, storage_bucket, storage_key, storage_etag, storage_size,
              storage_status, change_type, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (document_id, version) DO NOTHING
            """,
            [
                str(uuid.uuid4()),
                doc_id,
                version,
                metadata["content_hash"],
                metadata["content_length"],
                metadata["title"],
                metadata["source_file"],
                metadata["relative_path"],
                stored.storage_path,
                stored.storage_backend,
                stored.storage_bucket,
                stored.storage_key,
                stored.storage_etag,
                stored.storage_size,
                stored.storage_status,
                change_type,
                Jsonb({"doc_key": metadata["doc_key"]}),
            ],
        )


def to_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def build_library_search_config(domain: str, cfg: dict[str, Any]) -> dict[str, Any]:
    retrieval_mode = (cfg.get("retrieval_mode") or os.getenv("CODING_RAG_RETRIEVAL_MODE") or "hybrid_rerank").strip().lower()
    if retrieval_mode not in {"semantic", "bm25", "hybrid", "hybrid_rerank"}:
        retrieval_mode = "hybrid_rerank"

    keyword_backend = os.getenv("CODING_RAG_KEYWORD_BACKEND", cfg.get("keyword_backend", "local_bm25")).strip().lower()
    opensearch_index = os.getenv(
        f"CODING_RAG_ES_INDEX_{domain.upper()}",
        cfg.get("es_index") or f"codingrag_{domain}_docs",
    )

    return {
        "retrieval_mode": retrieval_mode,
        "embedding_model": cfg.get("embedding_model"),
        "embedding_model_name": cfg.get("embedding_model_name"),
        "embedding_dim": cfg.get("embedding_dim"),
        "rerank_model_name": cfg.get("rerank_model_name"),
        "keyword_backend": keyword_backend,
        "qdrant_collection": cfg.get("collection"),
        "opensearch_index": opensearch_index if keyword_backend in {"elasticsearch", "opensearch", "es"} else None,
    }


def iter_document_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS and not path.name.startswith("."):
            yield path


def inspect_document(path: Path, root: Path, domain: str, cfg: dict[str, Any]) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="replace")
    relative_path = path.relative_to(root).as_posix()
    content_bytes = content.encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    title = derive_title(content, path)
    mime_type = mimetypes.guess_type(path.name)[0] or "text/plain"
    return {
        "domain": domain,
        "doc_key": f"{domain}:{relative_path}",
        "title": title,
        "source_file": relative_path,
        "local_path": str(path.resolve()),
        "relative_path": relative_path,
        "mime_type": mime_type,
        "language": cfg.get("language"),
        "content_hash": content_hash,
        "content_length": len(content_bytes),
        "metadata": {"suffix": path.suffix.lower()},
    }


def derive_title(content: str, path: Path) -> str:
    for line in content.splitlines()[:80]:
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:300]
    return path.stem.replace("-", " ").replace("_", " ")[:300] or path.name


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(to_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"archive missing {path.name}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _safe_slug(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z._=-]+", "-", (value or "library").strip())
    return value.strip(".-_") or "library"


def _export_file_path(version: dict[str, Any]) -> str:
    doc_key = _safe_slug(version.get("doc_key") or version.get("document_id") or "doc")
    rel = version.get("relative_path") or version.get("source_file") or "document.txt"
    suffix = Path(rel).suffix or ".txt"
    name = _safe_slug(Path(rel).name or f"v{version.get('version')}{suffix}")
    return f"files/{doc_key}/v{int(version.get('version') or 1):04d}-{name}"


def _validate_archive_member(name: str) -> str:
    if not name or "\x00" in name:
        raise ValueError("unsafe empty archive path")
    normalized = name.replace("\\", "/")
    p = Path(normalized)
    if p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise ValueError(f"unsafe archive path: {name}")
    return p.as_posix()


def _safe_extract_archive(archive: Path, target: Path) -> None:
    suffixes = "".join(archive.suffixes).lower()
    if suffixes.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                rel = _validate_archive_member(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"unsafe symlink in archive: {info.filename}")
                dest = (target / rel).resolve()
                if target.resolve() not in dest.parents and dest != target.resolve():
                    raise ValueError(f"archive path escapes target: {info.filename}")
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, dest.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        return
    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            rel = _validate_archive_member(member.name)
            if member.issym() or member.islnk() or member.isdev():
                raise ValueError(f"unsafe tar member: {member.name}")
            dest = (target / rel).resolve()
            if target.resolve() not in dest.parents and dest != target.resolve():
                raise ValueError(f"archive path escapes target: {member.name}")
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    raise ValueError(f"cannot read tar member: {member.name}")
                with src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def _retarget_doc_key(doc_key: str, source_code: str, target_code: str) -> str:
    prefix = f"{source_code}:"
    if doc_key.startswith(prefix):
        return f"{target_code}:{doc_key[len(prefix):]}"
    return f"{target_code}:{doc_key}"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS doc_libraries (
  id UUID PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT,
  domain TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'filesystem',
  source_uri TEXT,
  root_path TEXT,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  version TEXT NOT NULL DEFAULT '1.0.0',
  retrieval_mode TEXT NOT NULL DEFAULT 'hybrid_rerank',
  embedding_model TEXT,
  embedding_model_name TEXT,
  embedding_dim INTEGER,
  rerank_model_name TEXT,
  keyword_backend TEXT,
  qdrant_collection TEXT,
  opensearch_index TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (source_type IN ('filesystem', 'git', 'archive')),
  CHECK (retrieval_mode IN ('semantic', 'bm25', 'hybrid', 'hybrid_rerank'))
);
CREATE INDEX IF NOT EXISTS idx_doc_libraries_domain ON doc_libraries(domain);
CREATE INDEX IF NOT EXISTS idx_doc_libraries_enabled ON doc_libraries(enabled);

CREATE TABLE IF NOT EXISTS documents (
  id UUID PRIMARY KEY,
  library_id UUID NOT NULL REFERENCES doc_libraries(id),
  domain TEXT NOT NULL,
  doc_key TEXT NOT NULL,
  title TEXT NOT NULL,
  source_url TEXT,
  source_file TEXT,
  local_path TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  mime_type TEXT,
  language TEXT,
  content_hash TEXT NOT NULL,
  content_length INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  status TEXT NOT NULL DEFAULT 'new',
  indexed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at TIMESTAMPTZ,
  last_scanned_at TIMESTAMPTZ,
  scan_run_id UUID,
  index_required BOOLEAN NOT NULL DEFAULT TRUE,
  last_index_error_at TIMESTAMPTZ,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(library_id, doc_key),
  UNIQUE(library_id, relative_path),
  CHECK (status IN ('new', 'changed', 'indexed', 'failed', 'disabled', 'deleted'))
);
CREATE INDEX IF NOT EXISTS idx_documents_library_id ON documents(library_id);
CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_enabled ON documents(enabled);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_source_file ON documents(source_file);

CREATE TABLE IF NOT EXISTS document_versions (
  id UUID PRIMARY KEY,
  document_id UUID NOT NULL REFERENCES documents(id),
  version INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  content_length INTEGER NOT NULL DEFAULT 0,
  title TEXT,
  source_url TEXT,
  source_file TEXT,
  relative_path TEXT NOT NULL,
  storage_path TEXT,
  storage_backend TEXT NOT NULL DEFAULT 'local',
  storage_bucket TEXT,
  storage_key TEXT,
  storage_etag TEXT,
  storage_size BIGINT,
  storage_status TEXT NOT NULL DEFAULT 'active',
  expires_at TIMESTAMPTZ,
  change_type TEXT NOT NULL DEFAULT 'update',
  tombstone BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(document_id, version),
  CHECK (change_type IN ('create', 'update', 'delete', 'restore')),
  CHECK (storage_backend IN ('local', 'seaweedfs', 's3', 'minio', 'fastdfs')),
  CHECK (storage_status IN ('active', 'deleting', 'deleted', 'missing'))
);
CREATE INDEX IF NOT EXISTS idx_document_versions_document_id ON document_versions(document_id);
CREATE INDEX IF NOT EXISTS idx_document_versions_hash ON document_versions(content_hash);

CREATE TABLE IF NOT EXISTS library_transfer_jobs (
  id UUID PRIMARY KEY,
  library_id UUID REFERENCES doc_libraries(id),
  direction TEXT NOT NULL,
  archive_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  mode TEXT,
  dry_run BOOLEAN NOT NULL DEFAULT TRUE,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  CHECK (direction IN ('export', 'import')),
  CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  CHECK (mode IS NULL OR mode IN ('skip', 'upsert', 'replace-library', 'rename-library'))
);
CREATE INDEX IF NOT EXISTS idx_library_transfer_jobs_library_id ON library_transfer_jobs(library_id);
CREATE INDEX IF NOT EXISTS idx_library_transfer_jobs_status ON library_transfer_jobs(status);

CREATE TABLE IF NOT EXISTS document_retention_jobs (
  id UUID PRIMARY KEY,
  library_id UUID REFERENCES doc_libraries(id),
  document_id UUID REFERENCES documents(id),
  status TEXT NOT NULL DEFAULT 'pending',
  retention_versions INTEGER NOT NULL DEFAULT 2,
  dry_run BOOLEAN NOT NULL DEFAULT TRUE,
  summary JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_document_retention_jobs_library_id ON document_retention_jobs(library_id);
CREATE INDEX IF NOT EXISTS idx_document_retention_jobs_status ON document_retention_jobs(status);
"""

MIGRATION_SQL = """
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS retrieval_mode TEXT NOT NULL DEFAULT 'hybrid_rerank';
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS embedding_model_name TEXT;
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS embedding_dim INTEGER;
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS rerank_model_name TEXT;
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS keyword_backend TEXT;
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS qdrant_collection TEXT;
ALTER TABLE doc_libraries ADD COLUMN IF NOT EXISTS opensearch_index TEXT;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'doc_libraries_retrieval_mode_check'
  ) THEN
    ALTER TABLE doc_libraries
      ADD CONSTRAINT doc_libraries_retrieval_mode_check
      CHECK (retrieval_mode IN ('semantic', 'bm25', 'hybrid', 'hybrid_rerank'));
  END IF;
END $$;
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'document_versions_storage_backend_check'
  ) THEN
    ALTER TABLE document_versions DROP CONSTRAINT document_versions_storage_backend_check;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'document_versions_storage_backend_check'
  ) THEN
    ALTER TABLE document_versions
      ADD CONSTRAINT document_versions_storage_backend_check
      CHECK (storage_backend IN ('local', 'seaweedfs', 's3', 'minio', 'fastdfs'));
  END IF;
END $$;
"""
