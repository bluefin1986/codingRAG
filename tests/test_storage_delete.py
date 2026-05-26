import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.storage import LocalObjectStorage, SeaweedFSObjectStorage


class _Response:
    status_code = 204

    def raise_for_status(self) -> None:
        pass


class _Client:
    deleted_urls: list[str] = []

    def __init__(self, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def delete(self, url: str):
        self.deleted_urls.append(url)
        return _Response()


class StorageDeleteTest(unittest.TestCase):
    def test_local_delete_removes_existing_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guide.md"
            path.write_text("content", encoding="utf-8")

            LocalObjectStorage(directory).delete_object(str(path))

            self.assertFalse(path.exists())

    def test_seaweed_delete_uses_filer_object_url(self) -> None:
        _Client.deleted_urls = []
        storage = SeaweedFSObjectStorage("http://filer:8888")

        with patch("api.storage.httpx.Client", _Client):
            storage.delete_object(
                "http://filer:8888/codingrag-originals/libraries/a/guide.md",
                storage_key="codingrag-originals/libraries/a/guide.md",
            )

        self.assertEqual(
            _Client.deleted_urls,
            ["http://filer:8888/codingrag-originals/libraries/a/guide.md"],
        )


if __name__ == "__main__":
    unittest.main()
