#!/usr/bin/env python3
"""Convert NGINX official nginx.org XML documentation snapshot to Markdown."""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from config import get_domain_config  # noqa: E402

VERSION = "official-snapshot"
SOURCE_REPO = "https://github.com/nginx/nginx.org"
SOURCE_REF = "main"
TREE_API_URL = "https://api.github.com/repos/nginx/nginx.org/git/trees/main?recursive=1"
RAW_BASE = "https://raw.githubusercontent.com/nginx/nginx.org/main"
HTML_BASE = "https://nginx.org/en/docs"


def fetch_text(url: str, retries: int = 3, timeout: int = 30) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "codingRAG-doc-converter/1.0"})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(attempt)
    raise RuntimeError(f"fetch failed after {retries} attempts: {url}: {last}")


def safe_clean(out_dir: Path) -> int:
    expected = (PROJECT_ROOT.parent / "nginx-docs-md" / "nginx").resolve()
    target = out_dir.resolve()
    if target != expected and "nginx" not in target.parts:
        raise RuntimeError(f"refusing to clean unexpected NGINX output dir: {target}")
    removed = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.md"):
        path.unlink()
        removed += 1
    return removed


def discover_xml_docs() -> dict[str, str]:
    tree = json.loads(fetch_text(TREE_API_URL)).get("tree", [])
    docs: dict[str, str] = {}
    prefix = "xml/en/docs/"
    for item in tree:
        path = item.get("path", "")
        if item.get("type") != "blob" or not path.startswith(prefix) or not path.endswith(".xml"):
            continue
        rel = path[len(prefix):]
        slug = rel[:-4].replace("/", "__")
        docs[slug] = f"{RAW_BASE}/{path}"
    if not docs:
        raise RuntimeError("no NGINX XML docs discovered from nginx/nginx.org")
    return dict(sorted(docs.items()))


def strip_tags(value: str) -> str:
    return re.sub(r"(?is)<[^>]+>", "", value)


def nginx_markup_to_markdown(raw: str) -> str:
    text = raw
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", text)
    text = re.sub(r"(?is)<programlisting[^>]*>(.*?)</programlisting>", lambda m: "\n```nginx\n" + html.unescape(strip_tags(m.group(1))).strip() + "\n```\n", text)
    text = re.sub(r"(?is)<example[^>]*>(.*?)</example>", lambda m: "\n```nginx\n" + html.unescape(strip_tags(m.group(1))).strip() + "\n```\n", text)
    text = re.sub(r"(?is)<syntax[^>]*>(.*?)</syntax>", lambda m: "\n**Syntax:** `" + html.unescape(strip_tags(m.group(1))).strip() + "`\n", text)
    text = re.sub(r"(?is)<default[^>]*>(.*?)</default>", lambda m: "\n**Default:** `" + html.unescape(strip_tags(m.group(1))).strip() + "`\n", text)
    text = re.sub(r"(?is)<context[^>]*>(.*?)</context>", lambda m: "\n**Context:** " + html.unescape(strip_tags(m.group(1))).strip() + "\n", text)
    text = re.sub(r"(?is)<directive[^>]*name=\"([^\"]+)\"[^>]*>", lambda m: "\n## Directive: " + m.group(1) + "\n", text)
    text = re.sub(r"(?is)<section[^>]*name=\"([^\"]+)\"[^>]*>", lambda m: "\n## " + m.group(1) + "\n", text)
    text = re.sub(r"(?is)<para[^>]*>|</para>", "\n\n", text)
    text = re.sub(r"(?is)<listitem[^>]*>(.*?)</listitem>", lambda m: "\n- " + strip_tags(m.group(1)).strip(), text)
    text = re.sub(r"(?is)<pre[^>]*>(.*?)</pre>", lambda m: "\n```nginx\n" + html.unescape(strip_tags(m.group(1))).strip() + "\n```\n", text)
    for level, tag in [(1, "h1"), (2, "h2"), (3, "h3"), (4, "h4")]:
        text = re.sub(fr"(?is)<{tag}[^>]*>(.*?)</{tag}>", lambda m, level=level: "\n" + "#" * level + " " + strip_tags(m.group(1)).strip() + "\n", text)
    text = re.sub(r"(?is)<li[^>]*>(.*?)</li>", lambda m: "\n- " + strip_tags(m.group(1)).strip(), text)
    text = re.sub(r"(?is)</p\s*>", "\n\n", text)
    text = strip_tags(text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def frontmatter(slug: str, source_url: str, doc_set: str) -> str:
    return "\n".join([
        "---",
        "product: nginx",
        f"version: {VERSION}",
        f"source_url: {source_url}",
        f"source_repo: {SOURCE_REPO}",
        f"source_ref: {SOURCE_REF}",
        f"doc_set: {doc_set}",
        f"slug: {slug}",
        "---",
        "",
    ])


def fallback_html_url(slug: str) -> str:
    return f"{HTML_BASE}/{slug.replace('__', '/')}.html"


def make_doc(slug: str, source_url: str, body: str, doc_set: str) -> str:
    return "\n".join([
        frontmatter(slug, source_url, doc_set),
        f"# NGINX official docs: {slug.replace('__', ' / ').replace('_', ' ').title()}",
        "",
        nginx_markup_to_markdown(body),
        "",
    ]).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", nargs="+", default=None, help="Optional NGINX doc slugs; default discovers all xml/en/docs/*.xml")
    parser.add_argument("--out-dir", type=Path, default=get_domain_config("nginx")["docs_dir"])
    parser.add_argument("--clean", action="store_true", help="Remove existing .md files in this NGINX output dir before writing")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.clean:
        print(f"Cleaned {safe_clean(args.out_dir)} existing NGINX Markdown files from {args.out_dir}")

    docs = discover_xml_docs()
    if args.docs:
        docs = {slug: docs[slug] for slug in args.docs}

    written: list[Path] = []
    failures: list[str] = []
    for slug, source_url in docs.items():
        doc_set = "xml"
        try:
            body = fetch_text(source_url)
            converted = nginx_markup_to_markdown(body)
            if len(converted) < 120:
                raise RuntimeError(f"converted XML body too short ({len(converted)} chars)")
        except Exception:
            try:
                source_url = fallback_html_url(slug)
                doc_set = "html"
                body = fetch_text(source_url)
                if len(nginx_markup_to_markdown(body)) < 120:
                    raise RuntimeError("converted HTML fallback body too short")
            except Exception as exc:
                failures.append(f"{slug}: {exc}")
                continue
        out_path = args.out_dir / f"{slug}.md"
        out_path.write_text(make_doc(slug, source_url, body, doc_set), encoding="utf-8")
        written.append(out_path)
    print(f"Wrote {len(written)} NGINX Markdown files to {args.out_dir}; failures={len(failures)}")
    for failure in failures[:20]:
        print(f"warning: {failure}", file=sys.stderr)
    if failures and not written:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
