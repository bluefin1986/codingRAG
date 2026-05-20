#!/usr/bin/env python3
"""Convert Redis 6.2.22 command metadata to Markdown for codingRAG.

Default mode discovers all Redis 6.2.22 command JSON files from the official
redis/redis tag and verifies each command exists in the Redis 6.2.22 server.c
command table. This avoids accidentally importing Redis 7/8-only commands.
"""
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

VERSION = "6.2.22"
SOURCE_REPO = "https://github.com/redis/redis"
SOURCE_REF = VERSION
COMMANDS_API_URL = f"https://api.github.com/repos/redis/redis/contents/src/commands?ref={VERSION}"
SERVER_C_URL = f"https://raw.githubusercontent.com/redis/redis/{VERSION}/src/server.c"


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
    expected = (PROJECT_ROOT.parent / "redis-docs-md" / "redis62").resolve()
    target = out_dir.resolve()
    if target != expected and "redis62" not in target.parts:
        raise RuntimeError(f"refusing to clean unexpected Redis output dir: {target}")
    removed = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.md"):
        path.unlink()
        removed += 1
    return removed


def discover_commands_from_json_dir() -> list[str]:
    """Redis 7+ has src/commands JSON. Redis 6.2 does not; caller falls back."""
    data = json.loads(fetch_text(COMMANDS_API_URL))
    commands = []
    for item in data:
        name = item.get("name", "")
        if name.endswith(".json") and item.get("type") == "file":
            commands.append(name[:-5].lower())
    return sorted(set(commands))


def server_command_rows(server_c: str) -> dict[str, str]:
    """Return command -> raw redisCommandTable row from Redis 6.2 server.c."""
    rows: dict[str, str] = {}
    start = server_c.find("struct redisCommand redisCommandTable[] = {")
    if start < 0:
        raise RuntimeError("redisCommandTable not found in server.c")
    end = server_c.find("};", start)
    if end < 0:
        raise RuntimeError("redisCommandTable end not found in server.c")
    table = server_c[start:end]
    for m in re.finditer(r'\{\s*"([a-z0-9_.-]+)"\s*,.*?\},', table, re.I | re.S):
        rows[m.group(1).lower()] = m.group(0).strip()
    return rows


def server_commands(server_c: str) -> set[str]:
    return set(server_command_rows(server_c))


def frontmatter(command: str, source_url: str) -> str:
    return "\n".join([
        "---",
        "product: redis",
        f"version: {VERSION}",
        f"source_url: {source_url}",
        f"source_repo: {SOURCE_REPO}",
        f"source_ref: {SOURCE_REF}",
        "doc_set: commands",
        f"command: {command.upper()}",
        "---",
        "",
    ])


def arg_lines(args: list[dict], depth: int = 0) -> list[str]:
    lines: list[str] = []
    indent = "  " * depth
    for arg in args or []:
        token = arg.get("token") or arg.get("name") or arg.get("type") or "argument"
        flags = []
        if arg.get("optional"):
            flags.append("optional")
        if arg.get("multiple"):
            flags.append("multiple")
        suffix = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"{indent}- `{token}`: {arg.get('type', '')}{suffix}".rstrip())
        if isinstance(arg.get("arguments"), list):
            lines.extend(arg_lines(arg["arguments"], depth + 1))
    return lines


def strip_tags(value: str) -> str:
    return re.sub(r"(?is)<[^>]+>", "", value)


def html_to_markdown(raw: str) -> str:
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", raw)
    text = re.sub(r"(?is)<pre[^>]*><code[^>]*>(.*?)</code></pre>", lambda m: "\n```\n" + html.unescape(strip_tags(m.group(1))).strip() + "\n```\n", text)
    text = re.sub(r"(?is)<pre[^>]*>(.*?)</pre>", lambda m: "\n```\n" + html.unescape(strip_tags(m.group(1))).strip() + "\n```\n", text)
    for level, tag in [(1, "h1"), (2, "h2"), (3, "h3"), (4, "h4")]:
        text = re.sub(fr"(?is)<{tag}[^>]*>(.*?)</{tag}>", lambda m, level=level: "\n" + "#" * level + " " + strip_tags(m.group(1)).strip() + "\n", text)
    text = re.sub(r"(?is)<li[^>]*>(.*?)</li>", lambda m: "\n- " + strip_tags(m.group(1)).strip(), text)
    text = re.sub(r"(?is)</p\s*>", "\n\n", text)
    text = html.unescape(strip_tags(text))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def command_to_markdown(command: str, data: dict, source_url: str, verified: bool) -> str:
    name = (data.get("command") or command).upper()
    lines = [frontmatter(command, source_url), f"# Redis {VERSION} command: {name}", ""]
    summary = data.get("summary")
    if summary:
        lines += [summary, ""]
    lines += ["## Metadata", ""]
    for key in ("since", "group", "complexity", "acl_categories"):
        if key in data:
            value = data[key]
            if isinstance(value, list):
                value = ", ".join(map(str, value))
            lines.append(f"- **{key}**: {value}")
    lines.append(f"- **Redis target version**: {VERSION}")
    lines.append(f"- **Version guard**: {'verified in Redis 6.2.22 server.c command table' if verified else 'not found in server.c; skipped unless explicitly requested'}")
    lines.append("- **Docs scope**: Redis 6.2.22 command JSON; do not include Redis 7/8-only commands.")
    lines.append("")
    if data.get("arguments"):
        lines += ["## Arguments", "", *arg_lines(data["arguments"]), ""]
    if data.get("reply_schema"):
        lines += ["## Reply schema", "", "```json", json.dumps(data["reply_schema"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def docs_page_to_markdown(command: str, body: str, source_url: str, verified: bool) -> str:
    return "\n".join([
        frontmatter(command, source_url),
        f"# Redis {VERSION} command: {command.upper()}",
        "",
        "## Metadata",
        "",
        f"- **Redis target version**: {VERSION}",
        f"- **Version guard**: {'verified in Redis 6.2.22 server.c command table' if verified else 'source verification unavailable'}",
        "- **Docs scope**: redis.io Redis command page fallback; avoid Redis 7/8-only commands.",
        "",
        html_to_markdown(body),
        "",
    ]).rstrip() + "\n"


def server_row_to_markdown(command: str, row: str, source_url: str) -> str:
    fields = [p.strip() for p in row.strip().strip("{},").split(",")]
    arity = fields[2] if len(fields) > 2 else "unknown"
    flags = fields[3].strip('"') if len(fields) > 3 else "unknown"
    key_specs = ", ".join(fields[6:9]) if len(fields) > 9 else "unknown"
    return "\n".join([
        frontmatter(command, source_url),
        f"# Redis {VERSION} command: {command.upper()}",
        "",
        "## Metadata",
        "",
        f"- **Redis target version**: {VERSION}",
        "- **Version guard**: verified in Redis 6.2.22 `src/server.c` command table.",
        "- **Docs scope**: generated from Redis 6.2.22 source metadata because structured command JSON is not present in Redis 6.2 and the redis.io page was unavailable.",
        f"- **Arity**: `{arity}`",
        f"- **Flags / ACL categories**: `{flags}`",
        f"- **Key spec fields**: `{key_specs}`",
        "",
        "## Source command table row",
        "",
        "```c",
        row,
        "```",
        "",
    ]).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commands", nargs="+", default=None, help="Optional Redis command JSON names, e.g. get set xreadgroup")
    parser.add_argument("--out-dir", type=Path, default=get_domain_config("redis62")["docs_dir"])
    parser.add_argument("--clean", action="store_true", help="Remove existing .md files in this Redis output dir before writing")
    parser.add_argument("--allow-unverified", action="store_true", help="Do not skip commands absent from server.c verification")
    parser.add_argument("--fetch-pages", action="store_true", help="Fetch redis.io pages when Redis 6.2 command JSON is unavailable; default uses source metadata only")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.clean:
        print(f"Cleaned {safe_clean(args.out_dir)} existing Redis Markdown files from {args.out_dir}")

    server_c = fetch_text(SERVER_C_URL)
    server_rows = server_command_rows(server_c)
    verified_set = set(server_rows)
    has_json_source = False
    if args.commands:
        commands = [c.lower() for c in args.commands]
    else:
        try:
            commands = discover_commands_from_json_dir()
            has_json_source = True
            print(f"Discovered {len(commands)} commands from Redis command JSON dir")
        except Exception as exc:
            commands = sorted(verified_set)
            print(f"Redis 6.2 has no src/commands JSON directory; discovered {len(commands)} commands from server.c ({exc})")

    written: list[Path] = []
    skipped: list[str] = []
    for command in commands:
        verified = command in verified_set
        if not verified and not args.allow_unverified:
            skipped.append(command)
            continue
        json_url = f"https://raw.githubusercontent.com/redis/redis/{VERSION}/src/commands/{command}.json"
        docs_url = f"https://redis.io/docs/latest/commands/{command}/"
        try:
            if not has_json_source:
                raise RuntimeError("Redis 6.2 has no src/commands JSON source")
            data = json.loads(fetch_text(json_url))
            markdown = command_to_markdown(command, data, json_url, verified)
        except Exception:
            if args.fetch_pages:
                try:
                    body = fetch_text(docs_url, retries=1, timeout=15)
                    markdown = docs_page_to_markdown(command, body, docs_url, verified)
                except Exception:
                    markdown = server_row_to_markdown(command, server_rows.get(command, ""), SERVER_C_URL)
            else:
                markdown = server_row_to_markdown(command, server_rows.get(command, ""), SERVER_C_URL)
        out_path = args.out_dir / f"command-{command}.md"
        out_path.write_text(markdown, encoding="utf-8")
        written.append(out_path)
    print(f"Wrote {len(written)} Redis Markdown files to {args.out_dir}; skipped_unverified={len(skipped)}")
    if skipped:
        print("Skipped commands absent from Redis 6.2.22 server.c: " + ", ".join(skipped[:50]))


if __name__ == "__main__":
    main()
