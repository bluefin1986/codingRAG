#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env.debug"

if [[ ! -r "$ENV_FILE" ]]; then
  printf 'Required debug environment file is not readable: %s\n' "$ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

exec "${PYTHON_BIN:-python3}" "$PROJECT_ROOT/scripts/library_import_worker.py" "$@"
