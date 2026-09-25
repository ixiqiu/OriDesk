#!/usr/bin/env bash
# ===========================================================================
# 新技元邮箱及工单管理系统 · 容器统一入口（web / worker / scheduler 共用）
#
#   web        等待 DB/Redis → migrate → collectstatic → exec gunicorn
#   worker     等待 DB/Redis → exec python manage.py rqworker default mail
#   scheduler  等待 DB/Redis → exec python manage.py runapscheduler
#
# 设计约定：
#   - set -Eeuo pipefail：任何一步失败立即退出，绝不带"半启动"状态对外服务；
#   - 幂等：migrate / collectstatic / mkdir 可重复执行，结果一致；
#   - 只有 web 角色执行 migrate，避免三个容器并发迁移互相等锁；
#   - 末尾 exec 接管 PID 1，docker stop 的 SIGTERM 能直达 gunicorn / rqworker；
#   - 与 config/settings.py 一致：数据库 / Redis 参数全部来自环境变量。
#
# 环境变量：
#   APP_ROLE=web|worker|scheduler  角色。compose 中显式注入；未注入时按启动命令自动识别。
#   DB_WAIT_TIMEOUT=60             等待数据库就绪上限（秒）
#   REDIS_WAIT_TIMEOUT=60          等待 Redis 就绪上限（秒）
#   SKIP_ENV_CHECK=1               跳过示例密钥占位检查（仅限本地调试）
#   MEDIA_ROOT / STATIC_ROOT       与 settings.py 相同的目录约定
# ===========================================================================
set -Eeuo pipefail

APP_HOME="${APP_HOME:-/app}"
APP_ROLE="${APP_ROLE:-}"
DB_WAIT_TIMEOUT="${DB_WAIT_TIMEOUT:-60}"
REDIS_WAIT_TIMEOUT="${REDIS_WAIT_TIMEOUT:-60}"
DB_ENGINE="${DB_ENGINE:-sqlite}"
# 与 config/settings.py 对齐：MEDIA_ROOT 可用环境变量覆盖，STATIC_ROOT 固定为 BASE_DIR/staticfiles
MEDIA_ROOT="${MEDIA_ROOT:-${APP_HOME}/media}"
STATIC_ROOT="${STATIC_ROOT:-${APP_HOME}/staticfiles}"

log() { printf '[entrypoint][%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { printf '[entrypoint][%s] ERROR: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

# 失败时给出一条可执行的排查指引；真实错误信息在其上方（各步骤自己的输出）。
trap 'printf "[entrypoint] 启动失败（退出码 %s）。排查步骤见 docs/部署手册.md 故障排查表。\n" "$?" >&2' ERR

cd "${APP_HOME}" || die "工作目录不存在：${APP_HOME}"

# ---------------------------------------------------------------- 角色识别
detect_role() {
  [ -n "${APP_ROLE}" ] && return 0
  case "$*" in
    *rqworker*)       APP_ROLE="worker" ;;
    *runapscheduler*) APP_ROLE="scheduler" ;;
    *)                APP_ROLE="web" ;;
  esac
}

# ---------------------------------------------------------------- 占位值防护
# 直接使用 .env.example 的示例值上线是常见事故：数据库能连、页面能开，
# 但 SECRET_KEY 可预测、FERNET_KEY 公开（等于邮箱授权码明文可解）。此处直接拒绝启动。
check_env() {
  if [ "${SKIP_ENV_CHECK:-0}" = "1" ]; then
    log "SKIP_ENV_CHECK=1：跳过密钥占位检查（仅限本地调试）"
    return 0
  fi

  local name value
  for name in DJANGO_SECRET_KEY FERNET_KEY; do
    value="${!name:-}"
    [ -n "${value}" ] || die "${name} 未设置：请在部署目录的 .env 中配置（模板见 .env.example）"
    case "${value}" in
      *请替换*|*请用*|*CHANGE_ME*|*changeme*|*CHANGEME*)
        die "${name} 仍是 .env.example 的示例值，请生成真实随机值（见 docs/部署手册.md §3）" ;;
    esac
  done

  if [ "${DB_ENGINE}" = "mysql" ] || [ "${DB_ENGINE}" = "mariadb" ]; then
    [ -n "${DB_PASSWORD:-}" ] || die "DB_PASSWORD 未设置（DB_ENGINE=${DB_ENGINE}）"
    case "${DB_PASSWORD}" in
      *请替换*|*CHANGE_ME*|*changeme*|*CHANGEME*)
        die "DB_PASSWORD 仍是示例值，请改为强随机密码（见 docs/部署手册.md §3）" ;;
    esac
  fi
}

# ---------------------------------------------------------------- 等待数据库
wait_for_db() {
  if [ "${DB_ENGINE}" != "mysql" ] && [ "${DB_ENGINE}" != "mariadb" ]; then
    log "DB_ENGINE=${DB_ENGINE}：非 MariaDB/MySQL，跳过数据库等待"
    return 0
  fi

  log "等待数据库 ${DB_HOST:-db}:${DB_PORT:-3306} 就绪（最长 ${DB_WAIT_TIMEOUT}s）…"
  local waited=0
  while true; do
    if python -c '
import os
import pymysql

conn = pymysql.connect(
    host=os.environ.get("DB_HOST", "db"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "ticket"),
    password=os.environ.get("DB_PASSWORD", ""),
    database=os.environ.get("DB_NAME", "ticket_system"),
    charset="utf8mb4",
    connect_timeout=5,
)
conn.close()
' >/dev/null 2>&1; then
      log "数据库已就绪（${DB_NAME:-ticket_system}@${DB_HOST:-db}）"
      return 0
    fi
    if [ "${waited}" -ge "${DB_WAIT_TIMEOUT}" ]; then
      die "等待数据库超时（${DB_WAIT_TIMEOUT}s）：检查 docker compose ps db 是否 healthy，以及 DB_NAME/DB_USER/DB_PASSWORD 是否正确"
    fi
    sleep 3
    waited=$((waited + 3))
  done
}

# ---------------------------------------------------------------- 等待 Redis
wait_for_redis() {
  if [ -z "${REDIS_URL:-}" ]; then
    log "REDIS_URL 未设置：跳过 Redis 等待"
    return 0
  fi

  log "等待 Redis ${REDIS_URL} 就绪（最长 ${REDIS_WAIT_TIMEOUT}s）…"
  local waited=0
  while true; do
    if python -c '
import os
import sys

import redis

client = redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=5, socket_timeout=5)
sys.exit(0 if client.ping() else 1)
' >/dev/null 2>&1; then
      log "Redis 已就绪"
      return 0
    fi
    if [ "${waited}" -ge "${REDIS_WAIT_TIMEOUT}" ]; then
      die "等待 Redis 超时（${REDIS_WAIT_TIMEOUT}s）：检查 docker compose ps redis 是否 healthy，以及 REDIS_URL 是否正确"
    fi
    sleep 3
    waited=$((waited + 3))
  done
}

# ---------------------------------------------------------------- 目录准备
prepare_dirs() {
  # 附件落盘目录：MEDIA_ROOT/attachments（开发文档 §6.3）
  if mkdir -p "${MEDIA_ROOT}/attachments" 2>/dev/null; then
    log "媒体目录就绪：${MEDIA_ROOT}/attachments"
  else
    die "无法创建媒体目录 ${MEDIA_ROOT}/attachments：宿主目录权限不足。请在宿主机执行 sudo chown -R 10001:10001 <宿主media目录> 后重试"
  fi
}

# ---------------------------------------------------------------- 迁移 / 静态
run_migrations() {
  log "执行数据库迁移：python manage.py migrate --noinput"
  python manage.py migrate --noinput
  log "迁移完成（幂等：已应用的迁移不会重复执行）"
}

collect_static() {
  log "收集静态文件：python manage.py collectstatic --noinput → ${STATIC_ROOT}"
  python manage.py collectstatic --noinput
  log "静态文件收集完成（由宿主 Nginx 直接从 ${STATIC_ROOT} 对应目录提供）"
}

# ---------------------------------------------------------------- 主流程
main() {
  [ "$#" -gt 0 ] || die "未提供启动命令：请通过 compose 的 command 指定，例如 gunicorn config.wsgi:application -b 0.0.0.0:8000"

  detect_role "$@"
  log "角色=${APP_ROLE}  启动命令：$*"

  check_env
  wait_for_db
  wait_for_redis

  case "${APP_ROLE}" in
    web)
      prepare_dirs
      run_migrations
      collect_static
      ;;
    worker|scheduler)
      # worker 需要写附件目录；scheduler 同步邮件时同样落盘。
      # 二者都不执行 migrate / collectstatic，避免与 web 并发迁移。
      prepare_dirs
      ;;
    *)
      die "未知 APP_ROLE=${APP_ROLE}（可选：web | worker | scheduler）"
      ;;
  esac

  log "启动：$*"
  exec "$@"
}

main "$@"
