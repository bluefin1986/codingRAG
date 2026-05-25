import unittest
from unittest.mock import patch

import psycopg

from api import registry as registry_module
from api.registry import DocumentRegistry, DomainCache
from config import get_domain_config


def _domain_config(collection: str) -> dict:
    return {
        "display_name": "Docs",
        "language": "en",
        "docs_dir": None,
        "collection": collection,
        "embedding_model": "BAAI/bge-m3",
        "embedding_model_name": "bge-m3",
        "embedding_dim": 1024,
        "rerank_model_name": "bge-reranker-base",
        "prompt_role": "expert",
        "bm25_enabled": True,
        "bm25_weight": 0.3,
        "path_boost_per_match": 0.0,
        "noise_patterns": [],
        "known_identifiers": [],
    }


class _RegistryWithoutSchemaInit(DocumentRegistry):
    def init_schema(self) -> None:
        pass


class _Connection:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        pass


class _ReindexCursor:
    def __init__(self) -> None:
        self._next = None
        self._claimed_item = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None) -> None:
        sql = " ".join(statement.split())
        if "UPDATE reindex_jobs" in sql and "RETURNING id" in sql:
            self._next = {"id": "job-1"}
        elif sql.startswith("SELECT status, index_target FROM reindex_jobs"):
            self._next = {"status": "running", "index_target": "vector"}
        elif "UPDATE reindex_items" in sql and "RETURNING document_id" in sql:
            if self._claimed_item:
                self._next = None
            else:
                self._claimed_item = True
                self._next = {"document_id": "doc-1"}
        elif sql.startswith("SELECT failed FROM reindex_jobs"):
            self._next = {"failed": 0}
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _ReindexRegistry(_RegistryWithoutSchemaInit):
    def __init__(self) -> None:
        super().__init__("postgresql://unused")
        self._cursor = _ReindexCursor()

    def _connect(self):
        return _Connection(self._cursor)

    def _refresh_reindex_summary(self, cur, job_id: str) -> None:
        pass

    def get_reindex_job(self, job_id: str) -> dict:
        return {"id": job_id, "status": "completed"}


class _IngestCursor:
    def __init__(self) -> None:
        self._next = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None) -> None:
        sql = " ".join(statement.split())
        if "UPDATE knowledge_ingest_jobs" in sql and "RETURNING *" in sql:
            self._next = {
                "id": "ingest-1",
                "domain": "docs",
                "source_type": "upload",
                "batch_size": 1,
            }
        elif sql.startswith("SELECT status FROM knowledge_ingest_jobs"):
            self._next = {"status": "running"}
        elif sql.startswith("SELECT COUNT(*) AS count FROM knowledge_ingest_items"):
            self._next = {"count": 0}
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _IngestRegistry(_RegistryWithoutSchemaInit):
    def __init__(self) -> None:
        super().__init__("postgresql://unused")
        self._cursor = _IngestCursor()
        self.observed_collection = None

    def _connect(self):
        return _Connection(self._cursor)

    def _run_ingest_items(self, job_id: str, *, batch_size: int) -> None:
        self.observed_collection = self._require_domain("docs")[1]["collection"]

    def _refresh_ingest_summary(self, cur, job_id: str) -> None:
        pass

    def get_ingest_job(self, job_id: str) -> dict:
        return {"id": job_id, "status": "completed"}


class DomainCacheRefreshTest(unittest.TestCase):
    def test_failed_job_refresh_preserves_existing_cached_domains(self) -> None:
        cache = DomainCache("postgresql://unused")
        cache._loaded = True
        cache._cache = {"existing": _domain_config("existing-v1")}

        with patch.object(
            cache,
            "_connect",
            side_effect=psycopg.OperationalError("temporarily unavailable"),
        ):
            cache.refresh()

        self.assertEqual(cache.get_config("existing")["collection"], "existing-v1")

    def test_unknown_library_fallback_does_not_reload_per_document(self) -> None:
        cache = DomainCache("postgresql://unused")
        cache._loaded = True
        cache._cache = {"existing": _domain_config("existing-v1")}

        with patch.object(cache, "load") as load:
            with self.assertRaises(KeyError):
                cache.get_config("library-only")
            with self.assertRaises(KeyError):
                cache.get_config("library-only")

        load.assert_not_called()

    def test_ingest_job_refreshes_new_domain_before_processing(self) -> None:
        cache = DomainCache("postgresql://unused")
        cache._loaded = True
        cache._cache = {}

        def load_new_domain() -> None:
            cache._loaded = True
            cache._cache["docs"] = _domain_config("new-collection")

        registry = _IngestRegistry()
        with patch.object(registry_module, "domain_cache", cache), patch.object(
            cache, "load", side_effect=load_new_domain
        ) as load:
            result = registry.run_ingest_job("ingest-1")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(registry.observed_collection, "new-collection")
        load.assert_called_once_with()

    def test_ingest_job_refreshes_cached_update_before_processing(self) -> None:
        cache = DomainCache("postgresql://unused")
        cache._loaded = True
        cache._cache = {"docs": _domain_config("stale-collection")}

        def load_updated_domain() -> None:
            cache._loaded = True
            cache._cache["docs"] = _domain_config("updated-collection")

        registry = _IngestRegistry()
        with patch.object(registry_module, "domain_cache", cache), patch.object(
            cache, "load", side_effect=load_updated_domain
        ) as load:
            result = registry.run_ingest_job("ingest-1")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(registry.observed_collection, "updated-collection")
        load.assert_called_once_with()

    def test_reindex_job_refreshes_cached_update_before_indexing(self) -> None:
        cache = DomainCache("postgresql://unused")
        cache._loaded = True
        cache._cache = {"docs": _domain_config("stale-collection")}
        observed_collections = []

        def load_updated_domain() -> None:
            cache._loaded = True
            cache._cache["docs"] = _domain_config("updated-collection")

        class _Indexer:
            def __init__(self, database_url: str) -> None:
                pass

            def index_document(self, document_id: str, *, target: str) -> dict:
                observed_collections.append(get_domain_config("docs")["collection"])
                return {"vector_indexed": True, "bm25_indexed": False}

        registry = _ReindexRegistry()
        with patch.object(registry_module, "domain_cache", cache), patch.object(
            cache, "load", side_effect=load_updated_domain
        ) as load, patch("indexer.per_doc_indexer.PerDocumentIndexer", _Indexer):
            result = registry.run_reindex_job("job-1")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed_collections, ["updated-collection"])
        load.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
