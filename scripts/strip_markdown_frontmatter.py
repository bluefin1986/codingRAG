#!/usr/bin/env python3
"""Strip leading YAML front matter from a Markdown directory.

By default this performs a dry run. Use --apply to update files in place.

Examples:
  python3 scripts/strip_markdown_frontmatter.py
  python3 scripts/strip_markdown_frontmatter.py --apply
  python3 scripts/strip_markdown_frontmatter.py /path/to/markdown --apply --verbose
"""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

DEFAULT_ROOT = Path('/Users/shadow/Downloads/output/markdown')
MARKDOWN_SUFFIXES = {'.md', '.markdown', '.mdx'}


def without_leading_frontmatter(content: str) -> str | None:
    """Return Markdown without an opening YAML front matter block, if present."""
    bom = '\ufeff' if content.startswith('\ufeff') else ''
    text = content[len(bom):]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        return None

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() not in {'---', '...'}:
            continue
        body = ''.join(lines[index + 1:])
        if body.startswith('\r\n'):
            body = body[2:]
        elif body.startswith('\n'):
            body = body[1:]
        return bom + body
    return None


def write_text_atomically(path: Path, content: str) -> None:
    mode = path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        newline='',
        dir=path.parent,
        prefix=f'.{path.name}.',
        suffix='.tmp',
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, mode)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def strip_frontmatter(path: Path, *, apply: bool) -> bool:
    content = path.read_text(encoding='utf-8')
    stripped = without_leading_frontmatter(content)
    if stripped is None:
        return False
    if apply:
        write_text_atomically(path, stripped)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description='Remove leading YAML front matter from Markdown files')
    parser.add_argument('root', nargs='?', type=Path, default=DEFAULT_ROOT, help=f'Markdown directory (default: {DEFAULT_ROOT})')
    parser.add_argument('--apply', action='store_true', help='Write changes in place; without this flag the script only reports matches')
    parser.add_argument('--verbose', action='store_true', help='Print every file containing leading YAML front matter')
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f'not a directory: {root}')

    files = sorted(path for path in root.rglob('*') if path.is_file() and path.suffix.lower() in MARKDOWN_SUFFIXES)
    changed = 0
    for path in files:
        if strip_frontmatter(path, apply=args.apply):
            changed += 1
            if args.verbose:
                print(path)

    action = 'updated' if args.apply else 'would update'
    print(f'{action} {changed} / {len(files)} Markdown files under {root}')
    if not args.apply and changed:
        print('Run again with --apply to write the changes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
