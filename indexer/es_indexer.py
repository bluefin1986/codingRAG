

"""Elasticsearch / OpenSearch indexing utilities for codingRAG.

This module writes chunk records into an external keyword index so production
keyword retrieval can use Elasticsearch/OpenSearch BM25 instead of rebuilding
an in-process BM25 index on every service restart.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

logger = logging.getLogger(__name__)


class ESIndexer:
    """Write codingRAG chunks into Elasticsearch/OpenSearch."""

    def __init__(
        self,
        *,
        base_url: str,
        index_name: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.index_name = index_name
        self.api_key = api_key
        self.client = httpx.Client(timeout=timeout)

    def _headers(self, *, ndjson: bool = False) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/x-ndjson" if ndjson else "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"ApiKey {self.api_key}"
        return headers

    def close(self) -> None:
        self.client.close()

    def ensure_index(self) -> None:
        """Create index if it does not already exist."""
        exists_resp = self.client.head(
            f"{self.base_url}/{self.index_name}",
            headers=self._headers(),
        )
        if exists_resp.status_code == 200:
            return
        if exists_resp.status_code not in (404,):
            exists_resp.raise_for_status()

        mapping = {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            },
            "mappings": {
                "properties": {
                    "domain": {"type": "keyword"},
                    "text": {"type": "text"},
                    "context": {"type": "text"},
                    "source_file": {"type": "keyword"},
                    "category": {"type": "keyword"},
                    "has_code": {"type": "boolean"},
                    "chunk_index": {"type": "integer"},
                    "chunk_pos": {"type": "integer"},
                }
            },
        }
        create_resp = self.client.put(
            f"{self.base_url}/{self.index_name}",
            headers=self._headers(),
            json=mapping,
        )
        create_resp.raise_for_status()
        logger.info("created keyword index %s", self.index_name)

    def delete_domain(self, domain: str) -> None:
        """Delete existing documents for one domain before re-indexing."""
        payload = {
            "query": {
                "term": {
                    "domain": domain,
                }
            }
        }
        resp = self.client.post(
            f"{self.base_url}/{self.index_name}/_delete_by_query",
            headers=self._headers(),
            json=payload,
        )
        if resp.status_code == 404:
            return
        resp.raise_for_status()
        logger.info("deleted existing keyword docs for domain=%s index=%s", domain, self.index_name)

    def index_chunks(self, chunks: Iterable[Dict[str, Any]], *, refresh: bool = True) -> int:
        """Bulk index chunk records.

        Each input record is expected to follow the existing chunks.jsonl shape:
        {
          "text": "...",
          "metadata": {...}
        }
        """
        lines: List[str] = []
        count = 0

        for pos, record in enumerate(chunks):
            metadata = record.get("metadata", {}) or {}
            domain = metadata.get("domain", "")
            source_file = metadata.get("source_file", "")
            chunk_index = int(metadata.get("chunk_index", pos) or 0)
            doc_id = self._doc_id(domain=domain, source_file=source_file, chunk_index=chunk_index, pos=pos)

            document = {
                "domain": domain,
                "text": record.get("text", "") or "",
                "context": metadata.get("context", "") or "",
                "source_file": source_file,
                "category": metadata.get("category", "") or "",
                "has_code": bool(metadata.get("has_code", False)),
                "chunk_index": chunk_index,
                "chunk_pos": pos,
            }

            lines.append(json.dumps({"index": {"_index": self.index_name, "_id": doc_id}}, ensure_ascii=False))
            lines.append(json.dumps(document, ensure_ascii=False))
            count += 1

        if not lines:
            return 0

        payload = "\n".join(lines) + "\n"
        bulk_resp = self.client.post(
            f"{self.base_url}/_bulk",
            headers=self._headers(ndjson=True),
            content=payload.encode("utf-8"),
        )
        bulk_resp.raise_for_status()
        bulk_data = bulk_resp.json()
        if bulk_data.get("errors"):
            errors = [item for item in bulk_data.get("items", []) if item.get("index", {}).get("error")]
            raise RuntimeError(f"ES bulk index failed: {errors[:3]}")

        if refresh:
            refresh_resp = self.client.post(
                f"{self.base_url}/{self.index_name}/_refresh",
                headers=self._headers(),
            )
            refresh_resp.raise_for_status()

        logger.info("indexed %d keyword docs into %s", count, self.index_name)
        return count

    def index_chunks_file(self, chunks_path: Path, *, domain: Optional[str] = None, clear_domain: bool = True) -> int:
        """Load chunks from a jsonl file and index them."""
        self.ensure_index()
        chunks = list(_read_chunks_jsonl(chunks_path))
        if not chunks:
            logger.warning("no chunks found in %s", chunks_path)
            return 0

        target_domain = domain or _infer_domain(chunks)
        if clear_domain and target_domain:
            self.delete_domain(target_domain)

        return self.index_chunks(chunks)

    @staticmethod
    def _doc_id(*, domain: str, source_file: str, chunk_index: int, pos: int) -> str:
        clean_source = source_file.replace("/", "_").replace(" ", "_") or "unknown"
        clean_domain = domain or "unknown"
        return f"{clean_domain}:{clean_source}:{chunk_index}:{pos}"


def _read_chunks_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _infer_domain(chunks: List[Dict[str, Any]]) -> Optional[str]:
    for record in chunks:
        metadata = record.get("metadata", {}) or {}
        domain = metadata.get("domain")
        if domain:
            return str(domain)
    return None