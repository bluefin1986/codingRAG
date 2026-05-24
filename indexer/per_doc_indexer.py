"""Incremental Qdrant indexing for one registry document at a time.

This Phase 3 path is independent from the legacy ``chunks.jsonl`` batch flow:
content is read from the current PostgreSQL document version, chunked in memory,
and replaced at the document boundary in Qdrant.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import psycopg
from psycopg.rows import dict_row

from api.registry import DocumentRegistry, RegistryUnavailable
from api.storage import create_storage
from chunker.parser import parse_blocks
from chunker.splitter import split_blocks
from config import (
    CHUNK_MAX_TOKENS,
    CHUNK_MIN_TOKENS,
    CHUNK_OVERLAP_TOKENS,
    CODING_RAG_DATABASE_URL,
    CODING_RAG_ES_API_KEY,
    CODING_RAG_ES_URL,
    CODING_RAG_SEAWEEDFS_BUCKET,
    CODING_RAG_SEAWEEDFS_FILER_URL,
    CODING_RAG_SEAWEEDFS_KEY_PREFIX,
    CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
    EMBEDDING_API_BASE,
    QDRANT_API_KEY,
    QDRANT_HOST,
    QDRANT_PORT,
    get_domain_config,
)
from indexer.es_indexer import ESIndexer
from indexer.qdrant_indexer import clean_text, embed_texts, truncate_text

logger = logging.getLogger(__name__)
POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "codingrag/per-document-index/v1")


class DocumentNotFound(LookupError):
    pass


class DocumentDisabled(ValueError):
    pass


class PerDocumentIndexer:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or CODING_RAG_DATABASE_URL
        if not self.database_url:
            raise RegistryUnavailable("CODING_RAG_DATABASE_URL is not configured")
        DocumentRegistry(self.database_url).init_schema()

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _load_document(self, doc_id: str) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.*, d.version AS document_version,
                       l.code AS library_code, l.qdrant_collection,
                       l.embedding_model AS library_embedding_model,
                       l.embedding_model_name AS library_embedding_model_name,
                       l.embedding_dim AS library_embedding_dim,
                       l.rerank_model_name AS library_rerank_model_name,
                       l.keyword_backend AS library_keyword_backend,
                       l.opensearch_index AS library_opensearch_index,
                       dv.storage_backend, dv.storage_bucket, dv.storage_key,
                       dv.storage_path, dv.storage_status,
                       COALESCE(dv.source_url, d.source_url) AS effective_source_url,
                       COALESCE(dv.source_file, d.source_file) AS effective_source_file,
                       COALESCE(dv.relative_path, d.relative_path) AS effective_relative_path
                FROM documents d
                JOIN doc_libraries l ON l.id = d.library_id
                JOIN document_versions dv
                  ON dv.document_id = d.id AND dv.version = d.version
                WHERE d.id = %s AND d.deleted_at IS NULL
                """,
                [doc_id],
            )
            row = cur.fetchone()
        if not row:
            raise DocumentNotFound(f"Document not found: {doc_id}")
        return row

    def _get_es_indexer(self, document: dict[str, Any]) -> ESIndexer | None:
        """Return an ESIndexer if ES is configured for this library, else None."""
        if not CODING_RAG_ES_URL:
            return None
        keyword_backend = str(document.get("library_keyword_backend") or "").strip().lower()
        if keyword_backend not in {"elasticsearch", "opensearch", "es"}:
            return None
        index_name = document.get("library_opensearch_index") or ""
        if not index_name:
            return None
        indexer = ESIndexer(
            base_url=CODING_RAG_ES_URL,
            index_name=index_name,
            api_key=CODING_RAG_ES_API_KEY or None,
        )
        indexer.ensure_index()
        return indexer

    def _build_es_chunks(self, document: dict[str, Any], chunks: list[Any]) -> list[dict[str, Any]]:
        """Build ES-compatible chunk records from parsed chunks."""
        category = self._category(document)
        records: list[dict[str, Any]] = []
        for chunk in chunks:
            chunk_index = int(chunk.metadata.get("chunk_index", len(records)))
            chunk_id = self._point_id(str(document["id"]), chunk_index)
            records.append({
                "doc_id": str(document["id"]),
                "library_id": str(document["library_id"]),
                "domain": document["domain"],
                "title": document["title"],
                "source_url": document.get("effective_source_url") or "",
                "source_file": document.get("effective_source_file") or "",
                "relative_path": document.get("effective_relative_path") or "",
                "content_hash": document["content_hash"],
                "document_version": int(document["document_version"]),
                "chunk_index": chunk_index,
                "chunk_id": chunk_id,
                "text": chunk.text,
                "context": chunk.metadata.get("context", ""),
                "has_code": bool(chunk.metadata.get("has_code", False)),
                "category": category,
            })
        return records

    @staticmethod
    def _domain_config(document: dict[str, Any]) -> dict[str, Any]:
        domain = str(document["domain"]).strip().lower()
        try:
            return get_domain_config(domain)
        except KeyError:
            collection = str(document.get("qdrant_collection") or "").strip()
            embedding_model_name = str(document.get("library_embedding_model_name") or "").strip()
            embedding_dim = document.get("library_embedding_dim")
            if not collection or not embedding_model_name or not embedding_dim:
                raise
            return {
                "domain": domain,
                "collection": collection,
                "embedding_model": document.get("library_embedding_model") or "BAAI/bge-m3",
                "embedding_model_name": embedding_model_name,
                "embedding_dim": int(embedding_dim),
                "rerank_model_name": document.get("library_rerank_model_name") or "bge-reranker-base",
                "noise_patterns": [],
            }

    @staticmethod
    def _collection(document: dict[str, Any], cfg: dict[str, Any]) -> str:
        return document.get("qdrant_collection") or cfg["collection"]

    @staticmethod
    def _category(document: dict[str, Any]) -> str:
        metadata = document.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("category"):
            return str(metadata["category"])
        first = Path(document.get("effective_relative_path") or "").parts[:1]
        return first[0] if first and first[0] in {"guides", "references"} else ""

    def _read_current_content(self, document: dict[str, Any]) -> str:
        storage = create_storage(
            document.get("storage_backend") or "local",
            seaweedfs_filer_url=CODING_RAG_SEAWEEDFS_FILER_URL,
            seaweedfs_public_base_url=CODING_RAG_SEAWEEDFS_PUBLIC_BASE_URL,
            seaweedfs_bucket=CODING_RAG_SEAWEEDFS_BUCKET,
            seaweedfs_key_prefix=CODING_RAG_SEAWEEDFS_KEY_PREFIX,
        )
        path = document.get("storage_path") or document.get("local_path") or ""
        return storage.read_text(path, storage_key=document.get("storage_key"), encoding="utf-8")

    @staticmethod
    def _qdrant_client() -> httpx.Client:
        headers = {"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else None
        return httpx.Client(
            base_url=f"http://{QDRANT_HOST}:{QDRANT_PORT}",
            headers=headers,
            timeout=120.0,
        )

    @staticmethod
    def _ensure_collection(client: httpx.Client, collection: str, embedding_dim: int) -> None:
        response = client.get(f"/collections/{collection}")
        if response.status_code == 200:
            return
        response = client.put(
            f"/collections/{collection}",
            json={"vectors": {"size": embedding_dim, "distance": "Cosine"}},
        )
        response.raise_for_status()

    @staticmethod
    def _delete_qdrant_points(client: httpx.Client, collection: str, doc_id: str) -> None:
        response = client.post(
            f"/collections/{collection}/points/delete",
            params={"wait": "true"},
            json={"filter": {"must": [{"key": "doc_id", "match": {"value": doc_id}}]}},
        )
        response.raise_for_status()

    @staticmethod
    def _upsert_points(client: httpx.Client, collection: str, points: list[dict[str, Any]]) -> None:
        if not points:
            return
        response = client.put(
            f"/collections/{collection}/points",
            params={"wait": "true"},
            json={"points": points},
        )
        response.raise_for_status()

    @staticmethod
    def _point_id(doc_id: str, chunk_index: int) -> str:
        return str(uuid.uuid5(POINT_NAMESPACE, f"{doc_id}:{chunk_index}"))

    def _build_points(
        self,
        document: dict[str, Any],
        chunks: list[Any],
        embeddings: list[list[float]],
    ) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        category = self._category(document)
        for chunk, vector in zip(chunks, embeddings):
            chunk_index = int(chunk.metadata.get("chunk_index", len(points)))
            chunk_id = self._point_id(str(document["id"]), chunk_index)
            points.append(
                {
                    "id": chunk_id,
                    "vector": vector,
                    "payload": {
                        "doc_id": str(document["id"]),
                        "library_id": str(document["library_id"]),
                        "domain": document["domain"],
                        "title": document["title"],
                        "source_url": document.get("effective_source_url") or "",
                        "source_file": document.get("effective_source_file") or "",
                        "relative_path": document.get("effective_relative_path") or "",
                        "content_hash": document["content_hash"],
                        "document_version": int(document["document_version"]),
                        "chunk_index": chunk_index,
                        "chunk_id": chunk_id,
                        "text": chunk.text,
                        "context": chunk.metadata.get("context", ""),
                        "has_code": bool(chunk.metadata.get("has_code", False)),
                        "category": category,
                    },
                }
            )
        return points

    def _mark_indexed(self, doc_id: str, chunk_count: int, embedding_model: str = "", embedding_model_name: str = "") -> None:
        with self._connect() as conn, conn.cursor() as cur:
            if embedding_model or embedding_model_name:
                cur.execute(
                    """
                    UPDATE documents
                    SET indexed_at = now(), chunk_count = %s, status = 'indexed',
                        error_message = NULL, index_required = FALSE, updated_at = now(),
                        embedding_model = COALESCE(%s, embedding_model),
                        embedding_model_name = COALESCE(%s, embedding_model_name)
                    WHERE id = %s AND deleted_at IS NULL
                    """,
                    [chunk_count, embedding_model or None, embedding_model_name or None, doc_id],
                )
            else:
                cur.execute(
                    """
                    UPDATE documents
                    SET indexed_at = now(), chunk_count = %s, status = 'indexed',
                        error_message = NULL, index_required = FALSE, updated_at = now()
                    WHERE id = %s AND deleted_at IS NULL
                    """,
                    [chunk_count, doc_id],
                )
            conn.commit()

    def _mark_failed(self, doc_id: str, error: Exception) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET status = 'failed', error_message = %s,
                    last_index_error_at = now(), index_required = TRUE, updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                """,
                [str(error)[:2000], doc_id],
            )
            conn.commit()

    def _mark_index_deleted(self, doc_id: str, enabled: bool) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE documents
                SET indexed_at = NULL, chunk_count = 0,
                    status = CASE WHEN %s THEN 'changed' ELSE status END,
                    index_required = CASE WHEN %s THEN TRUE ELSE index_required END,
                    updated_at = now()
                WHERE id = %s AND deleted_at IS NULL
                """,
                [enabled, enabled, doc_id],
            )
            conn.commit()

    def index_document(self, doc_id: str) -> dict[str, Any]:
        document = self._load_document(doc_id)
        if not document["enabled"]:
            raise DocumentDisabled(f"Document is disabled: {doc_id}")

        try:
            cfg = self._domain_config(document)
            collection = self._collection(document, cfg)
            content = self._read_current_content(document)
            chunks = split_blocks(
                parse_blocks(content),
                source_file=document.get("effective_source_file") or "",
                max_tokens=CHUNK_MAX_TOKENS,
                min_tokens=CHUNK_MIN_TOKENS,
                overlap_tokens=CHUNK_OVERLAP_TOKENS,
            )
            for chunk in chunks:
                chunk.text = clean_text(chunk.text, cfg.get("noise_patterns", []))
            chunks = [chunk for chunk in chunks if chunk.text.strip()]
            texts = [truncate_text(chunk.text) for chunk in chunks]
            embeddings = (
                embed_texts(
                    texts,
                    api_base=EMBEDDING_API_BASE,
                    model_name=cfg["embedding_model_name"],
                )
                if texts
                else []
            )
            if len(embeddings) != len(chunks):
                raise RuntimeError(f"Embedding count mismatch: chunks={len(chunks)} vectors={len(embeddings)}")
            points = self._build_points(document, chunks, embeddings)
            with self._qdrant_client() as client:
                self._ensure_collection(client, collection, int(cfg["embedding_dim"]))
                self._delete_qdrant_points(client, collection, doc_id)
                self._upsert_points(client, collection, points)
            # ES keyword index (skip silently if not configured)
            es_indexer = self._get_es_indexer(document)
            es_count = 0
            if es_indexer is not None:
                try:
                    es_indexer.delete_by_doc_id(doc_id)
                    es_chunks = self._build_es_chunks(document, chunks)
                    es_count = es_indexer.index_document_chunks(es_chunks)
                    es_indexer.close()
                except Exception:
                    logger.exception("ES indexing failed for doc_id=%s (Qdrant succeeded)", doc_id)
            self._mark_indexed(
                doc_id,
                len(points),
                embedding_model=cfg.get("embedding_model", ""),
                embedding_model_name=cfg.get("embedding_model_name", ""),
            )
            result: dict[str, Any] = {
                "doc_id": doc_id,
                "domain": document["domain"],
                "collection": collection,
                "document_version": int(document["document_version"]),
                "chunk_count": len(points),
                "status": "indexed",
            }
            if es_count:
                result["es_chunk_count"] = es_count
            return result
        except Exception as exc:
            self._mark_failed(doc_id, exc)
            raise

    def delete_document_index(self, doc_id: str) -> dict[str, Any]:
        document = self._load_document(doc_id)
        cfg = self._domain_config(document)
        collection = self._collection(document, cfg)
        with self._qdrant_client() as client:
            self._ensure_collection(client, collection, int(cfg["embedding_dim"]))
            self._delete_qdrant_points(client, collection, doc_id)
        # ES keyword index delete (skip silently if not configured)
        es_indexer = self._get_es_indexer(document)
        if es_indexer is not None:
            try:
                es_indexer.delete_by_doc_id(doc_id)
                es_indexer.close()
            except Exception:
                logger.exception("ES delete failed for doc_id=%s (Qdrant delete succeeded)", doc_id)
        self._mark_index_deleted(doc_id, bool(document["enabled"]))
        return {
            "doc_id": doc_id,
            "domain": document["domain"],
            "collection": collection,
            "status": "deleted-index",
            "index_required": bool(document["enabled"]),
        }

    def list_document_chunks(
        self,
        doc_id: str,
        *,
        limit: int = 50,
        offset: str | None = None,
    ) -> dict[str, Any]:
        """Read the current indexed chunk payloads for one document from Qdrant."""
        document = self._load_document(doc_id)
        collection = str(document.get("qdrant_collection") or "").strip()
        if not collection:
            cfg = self._domain_config(document)
            collection = self._collection(document, cfg)
        request: dict[str, Any] = {
            "filter": {"must": [{"key": "doc_id", "match": {"value": doc_id}}]},
            "limit": max(1, min(limit, 200)),
            "with_payload": True,
            "with_vector": False,
        }
        if offset:
            request["offset"] = offset

        with self._qdrant_client() as client:
            response = client.post(f"/collections/{collection}/points/scroll", json=request)
            if response.status_code == 404:
                points: list[dict[str, Any]] = []
                next_offset = None
            else:
                response.raise_for_status()
                result = response.json().get("result") or {}
                points = result.get("points") or []
                next_offset = result.get("next_page_offset")

        items = []
        for point in points:
            item = dict(point.get("payload") or {})
            item["point_id"] = str(point.get("id") or item.get("chunk_id") or "")
            items.append(item)
        items.sort(key=lambda item: int(item.get("chunk_index", 0)))
        return {
            "document_id": doc_id,
            "domain": document["domain"],
            "collection": collection,
            "total": int(document.get("chunk_count") or 0),
            "limit": request["limit"],
            "offset": offset,
            "next_offset": next_offset,
            "items": items,
        }

    def reindex_changed(self, domain: str) -> dict[str, Any]:
        normalized = domain.strip().lower()
        try:
            get_domain_config(normalized)
        except KeyError:
            raise ValueError(f"Unknown domain={normalized!r}") from None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM documents
                WHERE domain = %s AND index_required = TRUE AND enabled = TRUE AND deleted_at IS NULL
                ORDER BY updated_at, id
                """,
                [normalized],
            )
            doc_ids = [str(row["id"]) for row in cur.fetchall()]
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for doc_id in doc_ids:
            try:
                results.append(self.index_document(doc_id))
            except Exception as exc:
                logger.exception("Per-document indexing failed for doc_id=%s", doc_id)
                failures.append({"doc_id": doc_id, "error": str(exc)})
        return {
            "domain": normalized,
            "changed_only": True,
            "total": len(doc_ids),
            "indexed": len(results),
            "failed": len(failures),
            "results": results,
            "failures": failures,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Incremental document indexing from PostgreSQL registry to Qdrant")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--doc-id", help="Index one document UUID")
    action.add_argument("--delete-doc-id", help="Delete all indexed points for one document UUID")
    action.add_argument("--changed-only", action="store_true", help="Index only changed/enabled documents in --domain")
    parser.add_argument("--domain", help="Required together with --changed-only")
    args = parser.parse_args()
    if args.changed_only and not args.domain:
        parser.error("--domain is required with --changed-only")

    indexer = PerDocumentIndexer()
    if args.doc_id:
        result = indexer.index_document(args.doc_id)
    elif args.delete_doc_id:
        result = indexer.delete_document_index(args.delete_doc_id)
    else:
        result = indexer.reindex_changed(args.domain)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    raise SystemExit(main())
