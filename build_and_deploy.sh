#!/usr/bin/env bash
set -euo pipefail

# 自动加载当前目录下的 .env，便于统一配置密码、远端目录等参数
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# 参数解析
FULL_DEPLOY=false
for arg in "$@"; do
  case "$arg" in
    --full|-f) FULL_DEPLOY=true ;;
    --help|-h)
      echo "用法: $0 [--full]"
      echo "  --full, -f  完整部署（包含中间件：postgres, opensearch, qdrant, seaweedfs）"
      echo "  默认        只部署主 app（codingrag-api + workers）"
      exit 0
      ;;
  esac
done

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
TAR_NAME="${TAR_NAME:-codingrag-images.tar}"
REMOTE="${REMOTE:-codingRAG126_rag:/data/rag/container/}"

# 两层密码，从环境变量读取
PASS1="${RSYNC_PASS1:?请设置 RSYNC_PASS1}"
PASS2="${RSYNC_PASS2:?请设置 RSYNC_PASS2}"

# The app and workers are built from the same Dockerfile but use separate image
# tags. Ship all three exact references so the remote docker-compose only needs
# `docker load`, never a registry pull or a source-tree build.
CODINGRAG_VERSION="${CODINGRAG_VERSION:?请设置 CODINGRAG_VERSION}"
CODINGRAG_APP_IMAGE_REF="${CODINGRAG_IMAGE:-codingrag}:${CODINGRAG_VERSION}"
CODINGRAG_IMPORT_WORKER_IMAGE_REF="${CODINGRAG_IMPORT_WORKER_IMAGE:-codingrag-library-import-worker}:${CODINGRAG_VERSION}"
CODINGRAG_REINDEX_WORKER_IMAGE_REF="${CODINGRAG_REINDEX_WORKER_IMAGE:-codingrag-reindex-worker}:${CODINGRAG_VERSION}"

if [ "$FULL_DEPLOY" = true ]; then
  echo "==> 完整部署模式（包含中间件）"
  echo "==> 本机 docker compose build (all)"
  docker compose -f "$COMPOSE_FILE" build
  
  echo "==> 本机获取所有 compose 镜像"
  IMAGES=()
  while IFS= read -r image; do
    [ -n "$image" ] && IMAGES+=("$image")
  done < <(docker compose -f "$COMPOSE_FILE" config --images | sort -u)
else
  echo "==> 快速部署模式（只部署主 app）"
  echo "==> 本机 docker compose build app + workers"
  docker compose -f "$COMPOSE_FILE" build app library-import-worker reindex-worker
  
  # App and both workers use distinct Compose image tags; all must be loaded on
  # the remote host before legacy docker-compose starts the services.
  IMAGES=("$CODINGRAG_APP_IMAGE_REF" "$CODINGRAG_IMPORT_WORKER_IMAGE_REF" "$CODINGRAG_REINDEX_WORKER_IMAGE_REF")
fi

if [ "${#IMAGES[@]}" -eq 0 ]; then
  echo "未找到镜像"
  exit 1
fi

echo "==> 将以下镜像保存为 $TAR_NAME"
printf '  - %s\n' "${IMAGES[@]}"
docker save -o "$TAR_NAME" "${IMAGES[@]}"

echo "==> rsync 上传到 $REMOTE"

if [[ "$REMOTE" != *:* ]]; then
  echo "REMOTE 格式错误，应类似：docker126_rag:/data/rag/container/"
  exit 1
fi

REMOTE_HOST="${REMOTE%%:*}"
REMOTE_DIR="${REMOTE#*:}"
REMOTE_TAR_PATH="${REMOTE_DIR%/}/$TAR_NAME"
DEPLOY_DIR="${DEPLOY_DIR:-/data/rag/codingRAG}"

# 清除代理，避免 rsync/ssh 被拦截
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

PASS1="$PASS1" PASS2="$PASS2" TAR_NAME="$TAR_NAME" REMOTE="$REMOTE" /usr/bin/expect <<'EOF'
set timeout -1
set pass1 $env(PASS1)
set pass2 $env(PASS2)
set tar_name $env(TAR_NAME)
set remote $env(REMOTE)
set auth_count 0

spawn scp $tar_name $remote

expect {
  -re "(?i).*are you sure you want to continue connecting.*" {
    send -- "yes\r"
    exp_continue
  }
  -re "(?i).*(password|passphrase|密码|verification|second|二次|otp|code|动态|验证码|二级|第二).*" {
    incr auth_count
    if {$auth_count == 1} {
      send -- "$pass1\r"
    } else {
      send -- "$pass2\r"
    }
    exp_continue
  }
  eof
}
EOF

echo "==> ssh 到 $REMOTE_HOST 执行 docker load 和 docker-compose up"

if [ "$FULL_DEPLOY" = true ]; then
  # 完整部署：force-recreate 所有服务
  DEPLOY_CMD="cd '$DEPLOY_DIR' && docker load < '$REMOTE_TAR_PATH' && docker image inspect '$CODINGRAG_APP_IMAGE_REF' '$CODINGRAG_IMPORT_WORKER_IMAGE_REF' '$CODINGRAG_REINDEX_WORKER_IMAGE_REF' >/dev/null && docker-compose up -d --no-build --force-recreate"
else
  # 快速部署：只重启主 app 服务
  DEPLOY_CMD="cd '$DEPLOY_DIR' && docker load < '$REMOTE_TAR_PATH' && docker image inspect '$CODINGRAG_APP_IMAGE_REF' '$CODINGRAG_IMPORT_WORKER_IMAGE_REF' '$CODINGRAG_REINDEX_WORKER_IMAGE_REF' >/dev/null && docker-compose up -d --no-build --force-recreate app library-import-worker reindex-worker"
fi

PASS1="$PASS1" PASS2="$PASS2" REMOTE_HOST="$REMOTE_HOST" DEPLOY_CMD="$DEPLOY_CMD" /usr/bin/expect <<EOF
set timeout -1
set pass1 \$env(PASS1)
set pass2 \$env(PASS2)
set remote_host \$env(REMOTE_HOST)
set deploy_cmd \$env(DEPLOY_CMD)
set auth_count 0

spawn ssh \$remote_host \$deploy_cmd

expect {
  -re "(?i).*are you sure you want to continue connecting.*" {
    send -- "yes\r"
    exp_continue
  }
  -re "(?i).*(password|passphrase|密码|verification|second|二次|otp|code|动态|验证码|二级|第二).*" {
    incr auth_count
    if {\$auth_count == 1} {
      send -- "\$pass1\r"
    } else {
      send -- "\$pass2\r"
    }
    exp_continue
  }
  eof
}
EOF

echo "==> 完成"
