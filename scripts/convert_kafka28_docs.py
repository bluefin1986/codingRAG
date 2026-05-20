#!/usr/bin/env python3
"""Convert Apache Kafka 2.8.2 official documentation to Markdown."""
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

VERSION = "2.8.2"
ARTIFACT = "kafka_2.12-2.8.2"
SOURCE_REPO = "https://github.com/apache/kafka"
SOURCE_REF = VERSION
TREE_API_URL = f"https://api.github.com/repos/apache/kafka/git/trees/{VERSION}?recursive=1"
RAW_BASE = f"https://raw.githubusercontent.com/apache/kafka/{VERSION}"
SITE_BASE = "https://kafka.apache.org/28"
GENERATED_BASE = "https://kafka.apache.org/28/generated"
GENERATED_DOCS = {
    "topic_config": f"{GENERATED_BASE}/topic_config.html",
    "producer_config": f"{GENERATED_BASE}/producer_config.html",
    "consumer_config": f"{GENERATED_BASE}/consumer_config.html",
    "admin_client_config": f"{GENERATED_BASE}/admin_client_config.html",
    "kafka_config": f"{GENERATED_BASE}/kafka_config.html",
    "connect_config": f"{GENERATED_BASE}/connect_config.html",
    "connect_transforms": f"{GENERATED_BASE}/connect_transforms.html",
}


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
    expected = (PROJECT_ROOT.parent / "kafka-docs-md" / "kafka28").resolve()
    target = out_dir.resolve()
    if target != expected and "kafka28" not in target.parts:
        raise RuntimeError(f"refusing to clean unexpected Kafka output dir: {target}")
    removed = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.md"):
        path.unlink()
        removed += 1
    return removed


def discover_repo_docs() -> dict[str, tuple[str, str | None]]:
    tree = json.loads(fetch_text(TREE_API_URL)).get("tree", [])
    docs: dict[str, tuple[str, str | None]] = {}
    for item in tree:
        path = item.get("path", "")
        if item.get("type") != "blob" or not path.startswith("docs/"):
            continue
        if not path.endswith((".html", ".md")):
            continue
        slug = path[len("docs/"):].rsplit(".", 1)[0].replace("/", "__")
        site_url = f"{SITE_BASE}/{path[len('docs/'):] }" if path.endswith(".html") else None
        docs[slug] = (f"{RAW_BASE}/{path}", site_url)
    if not docs:
        raise RuntimeError("no Kafka docs discovered from apache/kafka tag")
    return dict(sorted(docs.items()))


def strip_tags(value: str) -> str:
    return re.sub(r"(?is)<[^>]+>", "", value)


def html_to_markdown(raw: str) -> str:
    text = raw
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<nav.*?</nav>", "", text)
    text = re.sub(r"(?is)<pre[^>]*><code[^>]*>(.*?)</code></pre>", lambda m: "\n```\n" + html.unescape(strip_tags(m.group(1))).strip() + "\n```\n", text)
    text = re.sub(r"(?is)<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + html.unescape(strip_tags(m.group(1))).strip() + "\n```\n", text)
    for level, tag in [(1, "h1"), (2, "h2"), (3, "h3"), (4, "h4"), (5, "h5")]:
        text = re.sub(fr"(?is)<{tag}[^>]*>(.*?)</{tag}>", lambda m, level=level: "\n" + "#" * level + " " + strip_tags(m.group(1)).strip() + "\n", text)
    text = re.sub(r"(?is)<tr[^>]*>", "\n", text)
    text = re.sub(r"(?is)</t[dh]>", " | ", text)
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
        "product: kafka",
        f"version: {VERSION}",
        f"artifact: {ARTIFACT}",
        f"source_url: {source_url}",
        f"source_repo: {SOURCE_REPO}",
        f"source_ref: {SOURCE_REF}",
        f"doc_set: {doc_set}",
        f"slug: {slug}",
        "---",
        "",
    ])


def make_doc(slug: str, source_url: str, body: str, doc_set: str) -> str:
    return "\n".join([
        frontmatter(slug, source_url, doc_set),
        f"# Apache Kafka {VERSION}: {slug.replace('__', ' / ').replace('_', ' ').title()}",
        "",
        f"Kafka artifact scope: `{ARTIFACT}`.",
        "",
        html_to_markdown(body),
        "",
    ]).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--docs", nargs="+", default=None, help="Optional Kafka doc slugs; default discovers all repo docs plus generated config docs")
    parser.add_argument("--out-dir", type=Path, default=get_domain_config("kafka28")["docs_dir"])
    parser.add_argument("--clean", action="store_true", help="Remove existing .md files in this Kafka output dir before writing")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.clean:
        print(f"Cleaned {safe_clean(args.out_dir)} existing Kafka Markdown files from {args.out_dir}")

    docs = discover_repo_docs()
    for slug, url in GENERATED_DOCS.items():
        docs.setdefault(slug, (url, None))
    if args.docs:
        docs = {slug: docs[slug] for slug in args.docs}

    written: list[Path] = []
    failures: list[str] = []
    for slug, (source_url, fallback_url) in docs.items():
        try:
            body = fetch_text(source_url)
            md_body = html_to_markdown(body)
            if len(md_body) < 120 and fallback_url:
                source_url = fallback_url
                body = fetch_text(source_url)
                md_body = html_to_markdown(body)
            if len(md_body) < 120:
                raise RuntimeError(f"converted body too short ({len(md_body)} chars)")
            doc_set = "generated" if "/generated/" in source_url else "docs"
            out_path = args.out_dir / f"{slug}.md"
            out_path.write_text(make_doc(slug, source_url, body, doc_set), encoding="utf-8")
            written.append(out_path)
        except Exception as exc:
            failures.append(f"{slug}: {exc}")
    print(f"Wrote {len(written)} Kafka Markdown files to {args.out_dir}; failures={len(failures)}")
    for failure in failures[:20]:
        print(f"warning: {failure}", file=sys.stderr)
    if failures and not written:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
