import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class _Uploader:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def upload(self, path: Path) -> str:
        self.paths.append(path)
        return f"https://files.example.com/assets/{path.name}"


class MarkdownAssetsTest(unittest.TestCase):
    def test_asset_storage_relative_path_uses_content_digest_not_source_path(self) -> None:
        from api.markdown_assets import asset_storage_relative_path

        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "assets" / "中文 文件名.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"png-content")

            key = asset_storage_relative_path(image_path)

        self.assertEqual(key, f"{hashlib.sha256(b'png-content').hexdigest()}.png")
        self.assertNotIn("assets", key)
        self.assertNotIn("中文", key)

    def test_rewrites_local_image_references_and_deduplicates_uploads(self) -> None:
        from api.markdown_assets import rewrite_local_markdown_images

        with TemporaryDirectory() as directory:
            root = Path(directory)
            markdown_path = root / "markdown" / "huawei" / "guide.md"
            image_path = root / "assets" / "diagram.png"
            markdown_path.parent.mkdir(parents=True)
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"png")
            markdown_path.write_text(
                "![flow](../../assets/diagram.png)\n![again](../../assets/diagram.png)\n![remote](https://example.com/image.png)",
                encoding="utf-8",
            )
            uploader = _Uploader()

            result = rewrite_local_markdown_images(markdown_path, root, uploader.upload)

            self.assertEqual(
                result.content,
                "![flow](https://files.example.com/assets/diagram.png)\n"
                "![again](https://files.example.com/assets/diagram.png)\n"
                "![remote](https://example.com/image.png)",
            )
            self.assertEqual(uploader.paths, [image_path.resolve()])
            self.assertEqual(result.stats, {"referenced": 2, "uploaded": 1, "reused": 1, "external": 1})

    def test_rejects_image_reference_outside_staging_root(self) -> None:
        from api.markdown_assets import MarkdownAssetError, rewrite_local_markdown_images

        with TemporaryDirectory() as directory:
            root = Path(directory)
            markdown_path = root / "markdown" / "guide.md"
            markdown_path.parent.mkdir(parents=True)
            markdown_path.write_text("![unsafe](../../../outside.png)", encoding="utf-8")

            with self.assertRaisesRegex(MarkdownAssetError, "escapes staging root"):
                rewrite_local_markdown_images(markdown_path, root, _Uploader().upload)


if __name__ == "__main__":
    unittest.main()
