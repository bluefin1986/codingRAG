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
QDRANT_API_KEY="${QDRANT_API_KEY:-QDRANT_API_KEY}"
COLLECTION="${COLLECTION:-ios_docs}"
DOMAIN="${DOMAIN:-ios}"

case "$DOMAIN" in
  ios)
    DEFAULT_QUERY="Objective-C UIButton 怎么响应点击事件"
    ;;
  harmonyos)
    DEFAULT_QUERY="HarmonyOS ArkUI Button 如何绑定点击事件"
    ;;
  *)
    DEFAULT_QUERY="Objective-C UIButton 怎么响应点击事件"
    ;;
esac

QUERY="${QUERY:-$DEFAULT_QUERY}"

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

run_post_json() {
  local name="$1"
  local url="$2"
  local payload="$3"

  local tmp
  tmp="$(mktemp)"

  local code
  code=$(curl -sS \
    -X POST "$url" \
    -H "Content-Type: application/json" \
    -o "$tmp" \
    -w "%{http_code}" \
    -d "$payload" || echo "000")

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

run_rag_query() {
  local name="$1"
  local url="$2"
  local payload="$3"

  local tmp
  tmp="$(mktemp)"

  local code
  code=$(curl -sS \
    -X POST "$url" \
    -H "Content-Type: application/json" \
    -o "$tmp" \
    -w "%{http_code}" \
    -d "$payload" || echo "000")

  echo "[$name] HTTP $code"

  if [[ ! "$code" =~ ^2 ]]; then
    cat "$tmp"
    echo
    record_result "$name" "FAIL" "http=$code"
    rm -f "$tmp"
    return
  fi

  local validation
  validation=$(python3 -c '
import json
import sys

path = sys.argv[1]

try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception as exc:
    print(f"FAIL|invalid_json:{exc}")
    raise SystemExit(0)

if "detail" in data:
    print(f"FAIL|detail:{data.get('detail')}")
    raise SystemExit(0)

results = data.get("results")
if not isinstance(results, list):
    print("FAIL|missing_results")
    raise SystemExit(0)

if len(results) == 0:
    print("FAIL|empty_results")
    raise SystemExit(0)

context = data.get("context", "")
if not isinstance(context, str) or not context.strip():
    print("FAIL|empty_context")
    raise SystemExit(0)

first = results[0]
source = first.get("source_file", "") if isinstance(first, dict) else ""
score = first.get("score", "") if isinstance(first, dict) else ""

print(f"PASS|results={len(results)},source={source},score={score}")
' "$tmp")

  local status detail
  status="${validation%%|*}"
  detail="${validation#*|}"

  if [[ "$status" == "PASS" ]]; then
    echo "OK $detail"
    record_result "$name" "PASS" "http=$code,$detail"
  else
    cat "$tmp"
    echo
    record_result "$name" "FAIL" "http=$code,$detail"
  fi

  rm -f "$tmp"
}

print_summary() {
  print_title "Summary"

  printf "%-8s %-32s %s\n" "STATUS" "CASE" "DETAIL"
  printf "%-8s %-32s %s\n" "------" "----" "------"

  for item in "${RESULTS[@]}"; do
    IFS='|' read -r status name detail <<< "$item"
    printf "%-8s %-32s %s\n" "$status" "$name" "$detail"
  done

  echo
  echo "PASS=${PASS_COUNT} FAIL=${FAIL_COUNT} TOTAL=$((PASS_COUNT + FAIL_COUNT))"

  if [[ "$FAIL_COUNT" -gt 0 ]]; then
    exit 1
  fi
}

print_title "codingRAG API regression test"

echo "BASE_URL=$BASE_URL"
echo "COLLECTION=$COLLECTION"
echo "DOMAIN=$DOMAIN"
echo "QUERY=$QUERY"

# ------------------------------------------------------------------
# 基础检查
# ------------------------------------------------------------------

run_check "openapi" "$BASE_URL/openapi.json"
run_check "docs" "$BASE_URL/docs"

# ------------------------------------------------------------------
# rag query
# ------------------------------------------------------------------

RAG_PAYLOAD=$(cat <<EOF
{
  "query": "${QUERY}",
  "domain": "${DOMAIN}",
  "topK": 5,
  "method": "hybrid",
  "hasCode": true
}
EOF
)

run_rag_query \
  "rag-query" \
  "$BASE_URL/api/v1/rag/query" \
  "$RAG_PAYLOAD"

# ------------------------------------------------------------------
# qdrant
# ------------------------------------------------------------------

check_qdrant() {
  local tmp
  tmp="$(mktemp)"

  local code
  code=$(curl -sS \
    -X GET "http://localhost:6333/collections" \
    -H "api-key: ${QDRANT_API_KEY}" \
    -o "$tmp" \
    -w "%{http_code}" || echo "000")

  echo "[qdrant-auth] HTTP $code"

  if [[ ! "$code" =~ ^2 ]]; then
    cat "$tmp"
    echo
    record_result "qdrant-auth" "FAIL" "http=$code"
    rm -f "$tmp"
    return
  fi

  local validation
  validation=$(python3 -c '
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
' "$tmp" "$COLLECTION")

  local status detail
  status="${validation%%|*}"
  detail="${validation#*|}"

  if [[ "$status" == "PASS" ]]; then
    echo "OK $detail"
    record_result "qdrant-auth" "PASS" "http=$code,$detail"
  else
    cat "$tmp"
    echo
    record_result "qdrant-auth" "FAIL" "http=$code,$detail"
  fi

  rm -f "$tmp"
}

check_qdrant

print_summary