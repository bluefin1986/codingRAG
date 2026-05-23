"""PostgreSQL-backed document registry for codingRAG v2 Phase 1."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from config import (
    CODING_RAG_DATABASE_URL,
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
