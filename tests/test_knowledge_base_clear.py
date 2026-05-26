import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app as app_module
from api.registry import DocumentRegistry, IngestStateConflict


DOCUMENT_ID = "3cb6657a-392e-45ae-8ce1-4aeb838ce9b3"


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


class _ClearCursor:
    def __init__(self, *, active_ingest: bool = False) -> None:
        self.active_ingest = active_ingest
        self.document = {
            "id": DOCUMENT_ID,
            "library_id": "library-1",
            "domain": "docs",
            "doc_key": "docs:guide.md",
            "relative_path": "guide.md",
            "content_hash": "same-hash",
            "content_length": 5,
            "version": 1,
            "status": "indexed",
            "enabled": True,
            "deleted_at": None,
            "index_required": False,
            "vector_index_required": False,
            "bm25_index_required": False,
        }
        self._one = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None) -> None:
        sql = " ".join(statement.split())
        self._one = None
        self._rows = []
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return
        if sql.startswith("SELECT EXISTS"):
            self._one = {"active_ingest": self.active_ingest, "active_reindex": False}
            return
        if sql.startswith("SELECT d.id FROM documents"):
            if self.document["deleted_at"] is None:
                self._rows = [{"id": DOCUMENT_ID}]
            return
        if sql.startswith("UPDATE documents SET enabled = FALSE"):
            if self.document["deleted_at"] is None:
                self.document.update(
                    {
                        "enabled": False,
                        "status": "deleted",
                        "deleted_at": "deleted",
                        "index_required": False,
                        "vector_index_required": False,
                        "bm25_index_required": False,
                    }
                )
                self._rows = [{"id": DOCUMENT_ID}]
            return
        if sql.startswith("SELECT * FROM documents WHERE library_id"):
            self._one = dict(self.document)
            return
        if sql.startswith("UPDATE documents SET title"):
            was_deleted = self.document["deleted_at"] is not None
            should_revive = was_deleted or "OR enabled = FALSE" in sql or "enabled = TRUE" in sql
            self.document.update(
                {
                    "enabled": True if should_revive else self.document["enabled"],
                    "status": "changed" if should_revive else self.document["status"],
                    "deleted_at": None,
                    "index_required": True if should_revive else self.document["index_required"],
                    "vector_index_required": True if should_revive else self.document["vector_index_required"],
                    "bm25_index_required": True if should_revive else self.document["bm25_index_required"],
                }
            )
            return
        if sql.startswith("UPDATE document_versions"):
            return
        if sql.startswith("SELECT COUNT(*) AS total FROM documents"):
            self._one = {"total": 1 if self.document["deleted_at"] is None else 0}
            return
        if sql.startswith("SELECT d.*, l.code AS library_code"):
            if self.document["deleted_at"] is None:
                self._rows = [dict(self.document)]

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _ClearRegistry(DocumentRegistry):
    def __init__(self, *, active_ingest: bool = False) -> None:
        super().__init__("postgresql://unused")
        self._cursor = _ClearCursor(active_ingest=active_ingest)

    def init_schema(self) -> None:
        pass

    def _connect(self):
        return _Connection(self._cursor)

    def _require_domain(self, domain: str) -> tuple[str, dict]:
        return domain.strip().lower(), {}


class KnowledgeBaseClearRegistryTest(unittest.TestCase):
    def test_clear_then_identical_reupload_revives_existing_document(self) -> None:
        registry = _ClearRegistry()
        prepared = registry.prepare_knowledge_base_documents_clear("docs")
        result = registry.soft_delete_knowledge_base_documents(
            "docs", [str(document["id"]) for document in prepared]
        )

        self.assertEqual(result["document_ids"], [DOCUMENT_ID])
        self.assertEqual(registry.list_knowledge_base_documents("docs")["total"], 0)

        action = registry._upsert_document(
            registry._cursor,
            "library-1",
            {
                "domain": "docs",
                "doc_key": "docs:guide.md",
                "title": "Guide",
                "source_file": "guide.md",
                "local_path": "/stored/guide.md",
                "relative_path": "guide.md",
                "mime_type": "text/markdown",
                "language": "en",
                "content_hash": "same-hash",
                "content_length": 5,
            },
            SimpleNamespace(
                storage_path="/stored/guide.md",
                storage_backend="local",
                storage_bucket=None,
                storage_key=None,
                storage_etag=None,
                storage_size=5,
                storage_status="active",
            ),
            "job-2",
        )

        documents = registry.list_knowledge_base_documents("docs")
        self.assertEqual(action, "unchanged")
        self.assertEqual(documents["total"], 1)
        self.assertEqual(documents["items"][0]["id"], DOCUMENT_ID)
        self.assertTrue(documents["items"][0]["enabled"])
        self.assertEqual(documents["items"][0]["status"], "changed")
        self.assertTrue(documents["items"][0]["index_required"])

    def test_clear_rejects_active_ingest_job(self) -> None:
        registry = _ClearRegistry(active_ingest=True)

        with self.assertRaisesRegex(IngestStateConflict, "active ingest"):
            registry.prepare_knowledge_base_documents_clear("docs")

    def test_identical_reupload_does_not_reenable_manually_disabled_document(self) -> None:
        registry = _ClearRegistry()
        registry._cursor.document.update({"enabled": False, "status": "disabled"})

        action = registry._upsert_document(
            registry._cursor,
            "library-1",
            {
                "domain": "docs",
                "doc_key": "docs:guide.md",
                "title": "Guide",
                "source_file": "guide.md",
                "local_path": "/stored/guide.md",
                "relative_path": "guide.md",
                "mime_type": "text/markdown",
                "language": "en",
                "content_hash": "same-hash",
                "content_length": 5,
            },
            SimpleNamespace(
                storage_path="/stored/guide.md",
                storage_backend="local",
                storage_bucket=None,
                storage_key=None,
                storage_etag=None,
                storage_size=5,
                storage_status="active",
            ),
            "job-2",
        )

        self.assertEqual(action, "unchanged")
        self.assertFalse(registry._cursor.document["enabled"])
        self.assertEqual(registry._cursor.document["status"], "disabled")
        self.assertFalse(registry._cursor.document["index_required"])


class _ApiRegistry:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy

    def create_knowledge_base_clear_job(self, domain: str) -> dict:
        if self.busy:
            raise IngestStateConflict("cannot clear knowledge base documents while active ingest jobs exist")
        return {"id": "clear-1", "domain": domain, "status": "pending", "total": 1}


class KnowledgeBaseClearApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)

    def test_delete_queues_background_clear(self) -> None:
        registry = _ApiRegistry()
        with patch.object(app_module, "_registry", registry):
            response = self.client.delete("/api/knowledge-bases/docs/documents")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "pending")

    def test_delete_returns_conflict_for_active_job(self) -> None:
        with patch.object(app_module, "_registry", _ApiRegistry(busy=True)):
            response = self.client.delete("/api/knowledge-bases/docs/documents")

        self.assertEqual(response.status_code, 409)
        self.assertIn("active ingest", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
