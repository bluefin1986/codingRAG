import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app as app_module
from api import registry as registry_module
from api.registry import DocumentRegistry, IngestStateConflict


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


class _ControlCursor:
    def __init__(self, *, status: str = "pending") -> None:
        self.status = status
        self.items = [
            {"document_id": "doc-1", "status": "pending"},
            {"document_id": "doc-2", "status": "pending"},
        ]
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None) -> None:
        sql = " ".join(statement.split())
        self._one = None
        if sql.startswith("SELECT status FROM reindex_jobs") and "FOR UPDATE" in sql:
            self._one = {"status": self.status}
            return
        if sql.startswith("UPDATE reindex_jobs SET status = 'pending'"):
            self.status = "pending"
            return
        if "UPDATE reindex_jobs" in sql and "SET status = 'running'" in sql and "RETURNING id" in sql:
            if self.status == "pending":
                self.status = "running"
                self._one = {"id": "job-1"}
            return
        if sql.startswith("SELECT status, index_target FROM reindex_jobs"):
            self._one = {"status": self.status, "index_target": "vector"}
            return
        if "UPDATE reindex_items" in sql and "RETURNING document_id" in sql:
            item = next((item for item in self.items if item["status"] == "pending"), None)
            if item is not None:
                item["status"] = "processing"
                self._one = {"document_id": item["document_id"]}
            return
        if sql.startswith("UPDATE reindex_items SET status = 'indexed'"):
            document_id = str(params[3])
            item = next(item for item in self.items if item["document_id"] == document_id)
            if item["status"] == "processing":
                item["status"] = "indexed"
            return
        if sql.startswith("UPDATE reindex_items SET status = 'failed'"):
            document_id = str(params[2])
            item = next(item for item in self.items if item["document_id"] == document_id)
            if item["status"] == "processing":
                item["status"] = "failed"
            return
        if sql.startswith("SELECT failed FROM reindex_jobs"):
            self._one = {"failed": sum(item["status"] == "failed" for item in self.items)}
            return
        if sql.startswith("SELECT status, failed FROM reindex_jobs"):
            self._one = {
                "status": self.status,
                "failed": sum(item["status"] == "failed" for item in self.items),
            }
            return
        if sql.startswith("UPDATE reindex_jobs SET status = %s"):
            required_status = str(params[3]) if "AND status = %s" in sql else None
            if required_status is None or self.status == required_status:
                self.status = str(params[0])

    def fetchone(self):
        return self._one


class _ControlRegistry(DocumentRegistry):
    def __init__(self, *, status: str = "pending") -> None:
        super().__init__("postgresql://unused")
        self._cursor = _ControlCursor(status=status)

    def init_schema(self) -> None:
        pass

    def _connect(self):
        return _Connection(self._cursor)

    def _refresh_reindex_summary(self, cur, job_id: str) -> None:
        pass

    def get_reindex_job(self, job_id: str) -> dict:
        return {
            "id": job_id,
            "status": self._cursor.status,
            "items": [dict(item) for item in self._cursor.items],
        }


class ReindexControlRegistryTest(unittest.TestCase):
    def test_transition_states_are_schema_valid_and_block_clear(self) -> None:
        for status in ("pausing", "paused", "cancelling"):
            self.assertIn(f"'{status}'", registry_module.REINDEX_SCHEMA_SQL)

        class _ActiveCursor:
            def __init__(self) -> None:
                self.sql = ""

            def execute(self, statement, params=None) -> None:
                self.sql = " ".join(statement.split())

            def fetchone(self):
                return {"active_ingest": False, "active_reindex": True, "active_clear": False}

        cursor = _ActiveCursor()
        with self.assertRaisesRegex(IngestStateConflict, "active reindex"):
            DocumentRegistry._raise_if_domain_jobs_active(cursor, "docs")
        for status in ("pausing", "paused", "cancelling"):
            self.assertIn(f"'{status}'", cursor.sql)

    def test_pause_resume_and_cancel_transitions(self) -> None:
        registry = _ControlRegistry()

        self.assertEqual(registry.pause_reindex_job("job-1")["status"], "paused")
        self.assertEqual(registry.resume_reindex_job("job-1")["status"], "pending")

        registry._cursor.status = "running"
        self.assertEqual(registry.pause_reindex_job("job-1")["status"], "pausing")
        self.assertEqual(registry.cancel_reindex_job("job-1")["status"], "cancelling")

        registry._cursor.status = "paused"
        self.assertEqual(registry.cancel_reindex_job("job-1")["status"], "cancelled")

        with self.assertRaisesRegex(ValueError, "pending or running"):
            registry.pause_reindex_job("job-1")
        with self.assertRaisesRegex(ValueError, "paused"):
            registry.resume_reindex_job("job-1")
        with self.assertRaisesRegex(ValueError, "pending, running, pausing or paused"):
            registry.cancel_reindex_job("job-1")

    def test_pause_stops_before_next_item_and_resume_only_indexes_pending_item(self) -> None:
        registry = _ControlRegistry()
        indexed: list[str] = []
        controls: list[str] = []

        class _Indexer:
            def __init__(self, database_url: str) -> None:
                pass

            def index_document(self, document_id: str, *, target: str) -> dict:
                indexed.append(document_id)
                if document_id == "doc-1":
                    controls.append(registry.pause_reindex_job("job-1")["status"])
                return {"vector_indexed": True, "bm25_indexed": False}

        with patch.object(registry_module.domain_cache, "refresh"), patch(
            "indexer.per_doc_indexer.PerDocumentIndexer", _Indexer
        ):
            paused = registry.run_reindex_job("job-1")
            self.assertEqual(paused["status"], "paused")
            self.assertEqual([item["status"] for item in paused["items"]], ["indexed", "pending"])

            registry.resume_reindex_job("job-1")
            completed = registry.run_reindex_job("job-1")

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(controls, ["pausing"])
        self.assertEqual(indexed, ["doc-1", "doc-2"])

    def test_cancel_stops_before_next_item_and_preserves_completed_result(self) -> None:
        registry = _ControlRegistry()
        indexed: list[str] = []
        controls: list[str] = []

        class _Indexer:
            def __init__(self, database_url: str) -> None:
                pass

            def index_document(self, document_id: str, *, target: str) -> dict:
                indexed.append(document_id)
                controls.append(registry.cancel_reindex_job("job-1")["status"])
                return {"vector_indexed": True, "bm25_indexed": False}

        with patch.object(registry_module.domain_cache, "refresh"), patch(
            "indexer.per_doc_indexer.PerDocumentIndexer", _Indexer
        ):
            cancelled = registry.run_reindex_job("job-1")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(controls, ["cancelling"])
        self.assertEqual(indexed, ["doc-1"])
        self.assertEqual([item["status"] for item in cancelled["items"]], ["indexed", "pending"])

    def test_pause_settles_after_in_flight_item_fails(self) -> None:
        registry = _ControlRegistry()
        controls: list[str] = []

        class _Indexer:
            def __init__(self, database_url: str) -> None:
                pass

            def index_document(self, document_id: str, *, target: str) -> dict:
                controls.append(registry.pause_reindex_job("job-1")["status"])
                raise RuntimeError("item indexing failed after pause request")

        with patch.object(registry_module.domain_cache, "refresh"), patch(
            "indexer.per_doc_indexer.PerDocumentIndexer", _Indexer
        ):
            paused = registry.run_reindex_job("job-1")

        self.assertEqual(controls, ["pausing"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual([item["status"] for item in paused["items"]], ["failed", "pending"])

    def test_cancel_settles_after_in_flight_item_fails(self) -> None:
        registry = _ControlRegistry()
        controls: list[str] = []

        class _Indexer:
            def __init__(self, database_url: str) -> None:
                pass

            def index_document(self, document_id: str, *, target: str) -> dict:
                controls.append(registry.cancel_reindex_job("job-1")["status"])
                raise RuntimeError("item indexing failed after cancel request")

        with patch.object(registry_module.domain_cache, "refresh"), patch(
            "indexer.per_doc_indexer.PerDocumentIndexer", _Indexer
        ):
            cancelled = registry.run_reindex_job("job-1")

        self.assertEqual(controls, ["cancelling"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual([item["status"] for item in cancelled["items"]], ["failed", "pending"])


class _ApiRegistry:
    def __init__(self) -> None:
        self.status = "pending"

    def pause_reindex_job(self, job_id: str) -> dict:
        if self.status not in {"pending", "running"}:
            raise ValueError("invalid pause")
        self.status = "pausing" if self.status == "running" else "paused"
        return {"id": job_id, "status": self.status}

    def resume_reindex_job(self, job_id: str) -> dict:
        if self.status != "paused":
            raise ValueError("invalid resume")
        self.status = "pending"
        return {"id": job_id, "status": self.status}

    def cancel_reindex_job(self, job_id: str) -> dict:
        if self.status not in {"pending", "running", "pausing", "paused"}:
            raise ValueError("invalid cancel")
        self.status = "cancelling" if self.status in {"running", "pausing"} else "cancelled"
        return {"id": job_id, "status": self.status}


class ReindexControlApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app_module.app)

    def test_control_routes_return_transitioned_job(self) -> None:
        registry = _ApiRegistry()
        with patch.object(app_module, "_registry", registry):
            self.assertEqual(self.client.post("/api/reindex-jobs/job-1/pause").json()["status"], "paused")
            self.assertEqual(self.client.post("/api/reindex-jobs/job-1/resume").json()["status"], "pending")
            self.assertEqual(self.client.post("/api/reindex-jobs/job-1/cancel").json()["status"], "cancelled")

    def test_running_control_routes_return_transitional_state(self) -> None:
        registry = _ApiRegistry()
        registry.status = "running"
        with patch.object(app_module, "_registry", registry):
            self.assertEqual(self.client.post("/api/reindex-jobs/job-1/pause").json()["status"], "pausing")
            self.assertEqual(self.client.post("/api/reindex-jobs/job-1/cancel").json()["status"], "cancelling")

    def test_control_routes_return_conflict_for_invalid_state(self) -> None:
        registry = _ApiRegistry()
        registry.status = "completed"
        with patch.object(app_module, "_registry", registry):
            for action in ("pause", "resume", "cancel"):
                response = self.client.post(f"/api/reindex-jobs/job-1/{action}")
                self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
