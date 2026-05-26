import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app as app_module
from api.registry import DocumentRegistry


class _Connection:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cursor


class _FailureCursor:
    def __init__(self) -> None:
        self.items = [
            {
                "id": f"item-{index}",
                "relative_path": f"docs/{index:04d}.md",
                "status": "completed",
                "error_message": None,
                "created_at": "created",
                "updated_at": "updated",
            }
            for index in range(500)
        ]
        self.items.append(
            {
                "id": "item-failed",
                "relative_path": "docs/zzzz-failed.md",
                "status": "failed",
                "error_message": "cannot read source",
                "created_at": "created-failed",
                "updated_at": "updated-failed",
            }
        )
        self._rows = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None) -> None:
        sql = " ".join(statement.split())
        self._rows = []
        self._one = None
        if sql.startswith("SELECT * FROM knowledge_ingest_jobs"):
            self._one = {"id": "job-1", "status": "failed"}
        elif sql.startswith("SELECT id FROM knowledge_ingest_jobs"):
            self._one = {"id": "job-1"}
        elif sql.startswith("SELECT COUNT(*)::int AS count"):
            self._one = {"count": 1}
        elif "WHERE job_id = %s AND status = 'failed'" in sql:
            limit, offset = params[1:]
            failed = [item for item in self.items if item["status"] == "failed"]
            self._rows = failed[offset : offset + limit]
        elif sql.startswith("SELECT id, relative_path, status, document_id"):
            self._rows = list(self.items[:500])

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _FailureRegistry(DocumentRegistry):
    def __init__(self) -> None:
        super().__init__("postgresql://unused")
        self._cursor = _FailureCursor()

    def init_schema(self) -> None:
        pass

    def _connect(self):
        return _Connection(self._cursor)


class IngestFailureRegistryTest(unittest.TestCase):
    def test_failure_after_first_500_items_is_queryable(self) -> None:
        registry = _FailureRegistry()

        snapshot = registry.get_ingest_job("job-1")
        failures = registry.list_ingest_job_failures("job-1", limit=20, offset=0)

        self.assertEqual(len(snapshot["items"]), 500)
        self.assertNotIn("docs/zzzz-failed.md", [item["relative_path"] for item in snapshot["items"]])
        self.assertEqual(failures["total"], 1)
        self.assertEqual(
            failures["items"][0],
            {
                "id": "item-failed",
                "relative_path": "docs/zzzz-failed.md",
                "status": "failed",
                "error_message": "cannot read source",
                "created_at": "created-failed",
                "updated_at": "updated-failed",
            },
        )


class _FailureApiRegistry:
    def list_ingest_job_failures(self, job_id: str, *, limit: int, offset: int) -> dict:
        return {
            "job_id": job_id,
            "status": "failed",
            "total": 1,
            "limit": limit,
            "offset": offset,
            "items": [{"relative_path": "docs/zzzz-failed.md", "error_message": "cannot read source"}],
        }


class IngestFailureApiTest(unittest.TestCase):
    def test_failure_endpoint_returns_paged_item_details(self) -> None:
        with patch.object(app_module, "_registry", _FailureApiRegistry()):
            response = TestClient(app_module.app).get("/api/ingest-jobs/job-1/failures?limit=25&offset=3")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["limit"], 25)
        self.assertEqual(payload["offset"], 3)
        self.assertEqual(payload["items"][0]["relative_path"], "docs/zzzz-failed.md")
        self.assertEqual(payload["items"][0]["error_message"], "cannot read source")


if __name__ == "__main__":
    unittest.main()
