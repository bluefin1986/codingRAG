#!/usr/bin/env bash
set -euo pipefail

# 自动加载当前目录下的 .env，便于统一配置密码、远端目录等参数
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
TAR_NAME="${TAR_NAME:-codingrag-images.tar}"
REMOTE="${REMOTE:-docker126_rag:/data/rag/container/}"

# 两层密码，从环境变量读取
PASS1="${RSYNC_PASS1:?请设置 RSYNC_PASS1}"
PASS2="${RSYNC_PASS2:?请设置 RSYNC_PASS2}"

echo "==> docker compose build"
docker compose -f "$COMPOSE_FILE" build

echo "==> 获取 compose 镜像列表"
IMAGES=()
while IFS= read -r image; do
  [ -n "$image" ] && IMAGES+=("$image")
done < <(docker compose -f "$COMPOSE_FILE" config --images | sort -u)

if [ "${#IMAGES[@]}" -eq 0 ]; then
  echo "未找到 compose 镜像"
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
DEPLOY_DIR="${DEPLOY_DIR:-/data/rag/codingrag}"

PASS1="$PASS1" PASS2="$PASS2" TAR_NAME="$TAR_NAME" REMOTE="$REMOTE" /usr/bin/expect <<'EOF'
set timeout -1
set pass1 $env(PASS1)
set pass2 $env(PASS2)
set tar_name $env(TAR_NAME)
set remote $env(REMOTE)
set auth_count 0

spawn rsync -avP $tar_name $remote

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

PASS1="$PASS1" PASS2="$PASS2" REMOTE_HOST="$REMOTE_HOST" DEPLOY_DIR="$DEPLOY_DIR" REMOTE_TAR_PATH="$REMOTE_TAR_PATH" /usr/bin/expect <<'EOF'
set timeout -1
set pass1 $env(PASS1)
set pass2 $env(PASS2)
set remote_host $env(REMOTE_HOST)
set deploy_dir $env(DEPLOY_DIR)
set remote_tar_path $env(REMOTE_TAR_PATH)
set auth_count 0

spawn ssh $remote_host "cd '$deploy_dir' && docker load < '$remote_tar_path' && docker-compose up -d --force-recreate"

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

echo "==> 完成"
