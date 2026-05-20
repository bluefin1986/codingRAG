#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

CODINGRAG_HTTP_HOST="${CODINGRAG_HTTP_HOST:-localhost}"
CODINGRAG_HTTP_PORT="${CODINGRAG_HTTP_PORT:-8060}"

BASE_URL="${BASE_URL:-http://${CODINGRAG_HTTP_HOST}:${CODINGRAG_HTTP_PORT}}"
QDRANT_URL="${QDRANT_URL:-http://localhost:6333}"
QDRANT_API_KEY="${QDRANT_API_KEY:-${CODING_RAG_QDRANT_API_KEY:-}}"

PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

print_title() {
  echo
  echo "========================================================================================"
  echo "$1"
  echo "========================================================================================"
}

record_result() {
  local name="$1"
  local status="$2"
  local detail="$3"

  RESULTS+=("${status}|${name}|${detail}")

  if [[ "$status" == "PASS" ]]; then
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

run_check() {
  local name="$1"
  local url="$2"

  local tmp
  tmp="$(mktemp)"

  local code
  code=$(curl -sS -o "$tmp" -w "%{http_code}" "$url" || echo "000")

  echo "[$name] HTTP $code"

  if [[ "$code" =~ ^2 ]]; then
    record_result "$name" "PASS" "http=$code"
  else
    cat "$tmp"
    echo
    record_result "$name" "FAIL" "http=$code"
  fi

  rm -f "$tmp"
}

json_payload() {
  local query="$1"
  local domain="$2"
  local top_k="$3"
  local method="$4"
  local has_code="$5"

  python3 - "$query" "$domain" "$top_k" "$method" "$has_code" <<'PY'
import json
import sys
query, domain, top_k, method, has_code = sys.argv[1:]
payload = {
    "query": query,
    "domain": domain,
    "topK": int(top_k),
    "method": method,
}
if has_code.lower() != "null":
    payload["hasCode"] = has_code.lower() == "true"
print(json.dumps(payload, ensure_ascii=False))
PY
}

run_rag_query() {
  local name="$1"
  local domain="$2"
  local query="$3"
  local method="$4"
  local top_k="$5"
  local has_code="$6"
  local expected_any_csv="$7"
  local expected_source_any_csv="${8:-}"

  local payload
  payload="$(json_payload "$query" "$domain" "$top_k" "$method" "$has_code")"

  local tmp
  tmp="$(mktemp)"

  local code
  code=$(curl -sS \
    -X POST "$BASE_URL/api/v1/rag/query" \
    -H "Content-Type: application/json" \
    -o "$tmp" \
    -w "%{http_code}" \
    -d "$payload" || echo "000")

  echo "[$name] HTTP $code"
  echo "  domain=$domain method=$method topK=$top_k query=$query"

  if [[ ! "$code" =~ ^2 ]]; then
    cat "$tmp"
    echo
    record_result "$name" "FAIL" "http=$code"
    rm -f "$tmp"
    return
  fi

  local validation
  validation=$(python3 - "$tmp" "$domain" "$expected_any_csv" "$expected_source_any_csv" <<'PY'
import json
import sys

path, expected_domain, expected_any_csv, expected_source_any_csv = sys.argv[1:]
expected_any = [x.strip().lower() for x in expected_any_csv.split(",") if x.strip()]
expected_source_any = [x.strip().lower() for x in expected_source_any_csv.split(",") if x.strip()]

try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception as exc:
    print(f"FAIL|invalid_json:{exc}")
    raise SystemExit(0)

if "detail" in data:
    print(f"FAIL|detail:{data.get('detail')}")
    raise SystemExit(0)

if data.get("domain") != expected_domain:
    print(f"FAIL|domain_mismatch:{data.get('domain')}!= {expected_domain}")
    raise SystemExit(0)

results = data.get("results")
if not isinstance(results, list):
    print("FAIL|missing_results")
    raise SystemExit(0)

if not results:
    print("FAIL|empty_results")
    raise SystemExit(0)

context = data.get("context", "")
if not isinstance(context, str) or not context.strip():
    print("FAIL|empty_context")
    raise SystemExit(0)

combined_parts = [context]
sources = []
for item in results:
    if not isinstance(item, dict):
        continue
    combined_parts.extend([
        str(item.get("text", "")),
        str(item.get("context", "")),
        str(item.get("source_file", "")),
    ])
    sources.append(str(item.get("source_file", "")))
combined = "\n".join(combined_parts).lower()
source_text = "\n".join(sources).lower()

missing_content = []
if expected_any and not any(term in combined for term in expected_any):
    missing_content.append("expected_any=" + "/".join(expected_any))
if expected_source_any and not any(term in source_text for term in expected_source_any):
    missing_content.append("expected_source_any=" + "/".join(expected_source_any))
if missing_content:
    print("FAIL|" + ";".join(missing_content))
    raise SystemExit(0)

first = results[0]
source = first.get("source_file", "") if isinstance(first, dict) else ""
score = first.get("score", "") if isinstance(first, dict) else ""
text = first.get("text", "") if isinstance(first, dict) else ""
preview = " ".join(str(text).split())[:220]
matched = [term for term in expected_any if term in combined][:5]
matched_sources = [term for term in expected_source_any if term in source_text][:5]
print(
    "PASS|"
    f"results={len(results)},source={source},score={score},"
    f"matched={matched},source_matched={matched_sources},preview={preview}"
)
PY
)

  local status detail
  status="${validation%%|*}"
  detail="${validation#*|}"

  if [[ "$status" == "PASS" ]]; then
    echo "  OK $detail"
    record_result "$name" "PASS" "http=$code,$detail"
  else
    echo "  REQUEST $payload"
    cat "$tmp"
    echo
    record_result "$name" "FAIL" "http=$code,$detail"
  fi

  rm -f "$tmp"
}

check_qdrant_collection() {
  local collection="$1"

  local tmp
  tmp="$(mktemp)"

  local headers=()
  if [[ -n "$QDRANT_API_KEY" ]]; then
    headers=(-H "api-key: ${QDRANT_API_KEY}")
  fi

  local code
  code=$(curl -sS \
    -X GET "$QDRANT_URL/collections" \
    "${headers[@]}" \
    -o "$tmp" \
    -w "%{http_code}" || echo "000")

  echo "[qdrant:$collection] HTTP $code"

  if [[ ! "$code" =~ ^2 ]]; then
    cat "$tmp"
    echo
    record_result "qdrant:$collection" "FAIL" "http=$code"
    rm -f "$tmp"
    return
  fi

  local validation
  validation=$(python3 - "$tmp" "$collection" <<'PY'
import json
import sys

path = sys.argv[1]
collection = sys.argv[2]

try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception as exc:
    print(f"FAIL|invalid_json:{exc}")
    raise SystemExit(0)

collections = data.get("result", {}).get("collections", [])
names = [item.get("name") for item in collections if isinstance(item, dict)]

if collection in names:
    print(f"PASS|collection={collection}")
else:
    print(f"FAIL|collection_not_found:{collection}")
PY
)

  local status detail
  status="${validation%%|*}"
  detail="${validation#*|}"

  if [[ "$status" == "PASS" ]]; then
    echo "  OK $detail"
    record_result "qdrant:$collection" "PASS" "http=$code,$detail"
  else
    cat "$tmp"
    echo
    record_result "qdrant:$collection" "FAIL" "http=$code,$detail"
  fi

  rm -f "$tmp"
}

print_summary() {
  print_title "Summary"

  printf "%-8s %-40s %s\n" "STATUS" "CASE" "DETAIL"
  printf "%-8s %-40s %s\n" "------" "----" "------"

  for item in "${RESULTS[@]}"; do
    IFS='|' read -r status name detail <<< "$item"
    printf "%-8s %-40s %s\n" "$status" "$name" "$detail"
  done

  echo
  echo "PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} TOTAL=$((PASS_COUNT + FAIL_COUNT))"

  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
  fi
}

print_title "codingRAG API regression test"

echo "BASE_URL=$BASE_URL"
echo "QDRANT_URL=$QDRANT_URL"

# ------------------------------------------------------------------
# 基础检查
# ------------------------------------------------------------------

run_check "health" "$BASE_URL/health"
run_check "openapi" "$BASE_URL/openapi.json"
run_check "docs" "$BASE_URL/docs"

# ------------------------------------------------------------------
# RAG query: smoke + golden-anchor checks
#
# 这些用例不是判断自然语言答案是否“完美”，而是用固定查询验证：
# 1) API 返回结构正确；2) 有非空 context/results；3) 命中了预期关键词/源文件锚点。
# ------------------------------------------------------------------

run_rag_query \
  "rag:ios:UIButton" \
  "ios" \
  "Objective-C UIButton 怎么响应点击事件" \
  "bm25" \
  5 \
  "true" \
  "UIButton,addTarget,touchUpInside" \
  "uibutton,ui_control"

run_rag_query \
  "rag:harmonyos:Button" \
  "harmonyos" \
  "HarmonyOS ArkUI Button 如何绑定点击事件" \
  "bm25" \
  5 \
  "true" \
  "Button,onClick,ArkUI" \
  "button,onclick"

run_rag_query \
  "rag:redis62:GET" \
  "redis62" \
  "Redis 6.2 GET command usage" \
  "bm25" \
  5 \
  "null" \
  "GET,Redis 6.2,command" \
  "get"

run_rag_query \
  "rag:redis62:XREADGROUP" \
  "redis62" \
  "Redis 6.2 XREADGROUP consumer group" \
  "bm25" \
  5 \
  "null" \
  "XREADGROUP,consumer,group" \
  "xreadgroup"

run_rag_query \
  "rag:kafka28:cleanup.policy" \
  "kafka28" \
  "Kafka 2.8 topic cleanup.policy configuration" \
  "bm25" \
  5 \
  "null" \
  "cleanup.policy,delete,compact" \
  "topic,config"

run_rag_query \
  "rag:kafka28:producer-acks" \
  "kafka28" \
  "Kafka 2.8 producer acks configuration" \
  "bm25" \
  5 \
  "null" \
  "acks,producer,all" \
  "producer,config"

run_rag_query \
  "rag:nginx:proxy_pass" \
  "nginx" \
  "nginx proxy_pass directive" \
  "bm25" \
  5 \
  "null" \
  "proxy_pass,proxy,Directive" \
  "proxy"

run_rag_query \
  "rag:nginx:try_files" \
  "nginx" \
  "nginx try_files directive" \
  "bm25" \
  5 \
  "null" \
  "try_files,uri,file" \
  "core,try_files"

run_rag_query \
  "rag:nginx:worker_processes" \
  "nginx" \
  "nginx worker_processes directive" \
  "bm25" \
  5 \
  "null" \
  "worker_processes,processes" \
  "core,worker_processes"

# ------------------------------------------------------------------
# Qdrant collection presence
# ------------------------------------------------------------------

check_qdrant_collection "ios_docs"
check_qdrant_collection "harmonyos_docs"
check_qdrant_collection "redis62_docs"
check_qdrant_collection "kafka28_docs"
check_qdrant_collection "nginx_docs"

print_summary
