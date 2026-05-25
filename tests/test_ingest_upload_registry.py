import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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


class _UploadCursor:
    def __init__(self) -> None:
        self.status = "accepting"
        self.items: dict[str, dict] = {}
        self._next = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None) -> None:
        sql = " ".join(statement.split())
        if sql.startswith("SELECT source_type, status FROM knowledge_ingest_jobs"):
            self._next = {"source_type": "upload", "status": self.status}
        elif sql.startswith("INSERT INTO knowledge_ingest_items"):
            self.items[params[2]] = {
                "relative_path": params[2],
                "source_path": params[3],
                "content_length": params[4],
            }
            self._next = None
        elif sql.startswith("SELECT COUNT(*)::int AS count FROM knowledge_ingest_items"):
            self._next = {"count": len(self.items)}
        elif sql.startswith("UPDATE knowledge_ingest_jobs SET status = 'pending'"):
            self.status = "pending"
            self._next = None
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _UploadRegistry(DocumentRegistry):
    def __init__(self) -> None:
        super().__init__("postgresql://unused")
        self._cursor = _UploadCursor()

    def init_schema(self) -> None:
        pass

    def _connect(self):
        return _Connection(self._cursor)

    def _refresh_ingest_summary(self, cur, job_id: str) -> None:
        pass

    def get_ingest_job(self, job_id: str) -> dict:
        return {
            "id": job_id,
            "status": self._cursor.status,
            "items": list(self._cursor.items.values()),
        }


class UploadIngestRegistryTest(unittest.TestCase):
    def test_mixed_batch_stages_only_non_hidden_documents(self) -> None:
        registry = _UploadRegistry()
        with TemporaryDirectory() as directory, patch.object(
            registry_module, "INGEST_STAGING_ROOT", Path(directory)
        ):
            result = registry.stage_ingest_files(
                "job-1",
                [
                    (".DS_Store", b"finder metadata"),
                    ("._foo.md", b"resource fork"),
                    (".xxxx.md", b"hidden text"),
                    (".git/config", b"git config"),
                    ("docs/.cache/a.md", b"cached"),
                    ("docs/guide.md", b"guide"),
                    ("README.md", b"readme"),
                ],
            )

            staged_root = Path(directory) / "job-1" / "uploads"
            self.assertEqual(
                [item["relative_path"] for item in result["items"]],
                ["docs/guide.md", "README.md"],
            )
            self.assertEqual((staged_root / "docs/guide.md").read_bytes(), b"guide")
            self.assertEqual((staged_root / "README.md").read_bytes(), b"readme")
            self.assertFalse((staged_root / ".DS_Store").exists())
            self.assertFalse((staged_root / "docs/.cache/a.md").exists())

    def test_all_hidden_batch_stages_nothing_and_job_remains_uploadable(self) -> None:
        registry = _UploadRegistry()
        with TemporaryDirectory() as directory, patch.object(
            registry_module, "INGEST_STAGING_ROOT", Path(directory)
        ):
            result = registry.stage_ingest_files(
                "job-1",
                [(".DS_Store", b"finder metadata"), ("docs/.cache/a.md", b"cached")],
            )

            self.assertEqual(result["status"], "accepting")
            self.assertEqual(result["items"], [])
            with self.assertRaisesRegex(IngestStateConflict, "without staged files"):
                registry.complete_ingest_upload("job-1")

            staged = registry.stage_ingest_files("job-1", [("docs/guide.md", b"guide")])
            self.assertEqual([item["relative_path"] for item in staged["items"]], ["docs/guide.md"])
            self.assertEqual(registry.complete_ingest_upload("job-1")["status"], "pending")

    def test_hidden_looking_traversal_is_still_rejected(self) -> None:
        registry = _UploadRegistry()
        with self.assertRaisesRegex(ValueError, "unsafe relative_path"):
            registry.stage_ingest_files("job-1", [("docs/.cache/../guide.md", b"unsafe")])

        self.assertEqual(registry.get_ingest_job("job-1")["items"], [])


if __name__ == "__main__":
    unittest.main()
