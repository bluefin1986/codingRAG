import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'strip_markdown_frontmatter.py'


def load_script_module():
    spec = importlib.util.spec_from_file_location('strip_markdown_frontmatter', SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load {SCRIPT_PATH}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StripMarkdownFrontmatterTest(unittest.TestCase):
    def test_strips_only_leading_yaml_frontmatter(self) -> None:
        module = load_script_module()
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'guide.md'
            path.write_text('---\nvendor: "xiaomi"\ntitle: "推送产品说明"\n---\n\n# 正文\n', encoding='utf-8')

            changed = module.strip_frontmatter(path, apply=True)

            self.assertTrue(changed)
            self.assertEqual(path.read_text(encoding='utf-8'), '# 正文\n')

    def test_does_not_strip_horizontal_rule_in_document_body(self) -> None:
        module = load_script_module()
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'guide.md'
            original = '# 标题\n\n---\n\n正文\n'
            path.write_text(original, encoding='utf-8')

            changed = module.strip_frontmatter(path, apply=True)

            self.assertFalse(changed)
            self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_dry_run_reports_change_without_writing(self) -> None:
        module = load_script_module()
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'guide.md'
            original = '---\nsource_id: "1533"\n---\n正文\n'
            path.write_text(original, encoding='utf-8')

            changed = module.strip_frontmatter(path, apply=False)

            self.assertTrue(changed)
            self.assertEqual(path.read_text(encoding='utf-8'), original)


if __name__ == '__main__':
    unittest.main()
