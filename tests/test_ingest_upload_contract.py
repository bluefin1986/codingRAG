import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app as app_module


class _FakeRegistry:
    def stage_ingest_files(self, job_id: str, payload: list[tuple[str, bytes]]) -> dict:
        return {
            "id": job_id,
            "items": [{"relative_path": relative_path} for relative_path, _ in payload],
        }


class UploadIngestFilesContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry_patch = patch.object(app_module, "_registry", _FakeRegistry())
        self.registry_patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self) -> None:
        self.registry_patch.stop()

    def test_accepts_repeated_relative_paths(self) -> None:
        response = self.client.post(
            "/api/ingest-jobs/qa-upload/files",
            files=[
                ("files", ("button.md", b"button", "text/markdown")),
                ("relative_paths", (None, "pkg/widgets/button.md")),
                ("files", ("client.md", b"client", "text/markdown")),
                ("relative_paths", (None, "pkg/network/client.md")),
            ],
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["relative_path"] for item in response.json()["items"]],
            ["pkg/widgets/button.md", "pkg/network/client.md"],
        )

    def test_rejects_relative_path_count_mismatch(self) -> None:
        response = self.client.post(
            "/api/ingest-jobs/qa-upload/files",
            files=[
                ("files", ("button.md", b"button", "text/markdown")),
                ("files", ("client.md", b"client", "text/markdown")),
                ("relative_paths", (None, "pkg/widgets/button.md")),
            ],
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "relative_paths count must match files count")


if __name__ == "__main__":
    unittest.main()
