#!/usr/bin/env bash
# ===========================================================================
# 新技元邮箱及工单管理系统 · 备份脚本
#
#   MariaDB 逻辑备份（mariadb-dump，单事务一致快照 + utf8mb4）
#   + media 目录打包（附件）
#   + SHA256 清单校验
#   + 按天滚动保留 N 天
#
# 用法：
#   scripts/backup.sh                                  # 常规备份
#   BACKUP_TAG=pre-restore scripts/backup.sh           # 恢复前的现状快照（脚本内部同款调用）
#   BACKUP_DIR=./backups scripts/backup.sh             # 本地开发（SQLite）演练
#
# 环境变量（可写在 .env，也可命令行传入；命令行/已有环境变量优先）：
#   APP_DIR=/data/dsh/home/OriDesk        项目目录（默认取脚本上一级）
#   BACKUP_DIR=/srv/ticket-system/backups 备份输出目录（权限 700）
#   BACKUP_RETENTION_DAYS=14              保留天数，按 mtime 清理 ticket-* 文件
#   HOST_MEDIA_DIR=/srv/ticket-system/media
#   COMPOSE_FILE=<项目目录>/docker-compose.yml
#   BACKUP_DB_HOST / BACKUP_DB_PORT       宿主机客户端备份时的连接地址（默认 127.0.0.1:3306）
#   BACKUP_INCLUDE_ENV=0                  1=同时把 .env 复制进备份目录（含 FERNET_KEY，谨慎）
#   DB_ENGINE=sqlite|mysql                默认取 .env；sqlite 走文件备份（本地开发）
#
# 产物（BACKUP_DIR 下）：
#   ticket-db-<时间戳>.sql.gz            MariaDB 逻辑备份
#   ticket-media-<时间戳>.tar.gz         media 目录（归档根为 media/）
#   ticket-<时间戳>.sha256               上述文件的校验和（restore.sh 恢复前会校验）
#
# 建议 crontab（每天 02:30；日志自行轮转）：
#   30 2 * * * cd /data/dsh/home/OriDesk && ./scripts/backup.sh >> /var/log/oridesk-backup.log 2>&1
#
# ⚠️ 安全与前提：
#   1) 备份含客户邮件正文、附件等个人信息，BACKUP_DIR 必须限权（本脚本 umask 077 + chmod 700）
#      并纳入加密/离线保存流程。
#   2) FERNET_KEY 不会随数据库一起"变得可用"——数据库里存的是密文。
#      请把 FERNET_KEY 单独保存在密码管理器中；丢失后所有邮箱授权码需重新录入。
#   3) 备份期间请避免执行数据库迁移（DDL 会被单事务快照排除，导致备份内的表结构与
#      数据版本不一致）。
# ===========================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-${APP_DIR}/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-/srv/ticket-system/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_INCLUDE_ENV="${BACKUP_INCLUDE_ENV:-0}"
BACKUP_TAG="${BACKUP_TAG:-}"

# 以下变量在 main() 里按 DB_ENGINE 与时间戳计算
STAMP=""
TAG_SUFFIX=""
DB_DUMP=""
MEDIA_TAR=""
MANIFEST=""

log() { printf '[backup][%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { printf '[backup][%s] ERROR: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- .env 解析
# 只解析 KEY=VALUE，不 eval、不执行任何内容（.env 可能含特殊字符与机密）。
# 与 config/settings.py::_load_dotenv 语义一致：已存在的环境变量优先。
load_env_file() {
  local file="$1" line key value
  [ -r "${file}" ] || return 0
  while IFS= read -r line || [ -n "${line}" ]; do
    line="${line%$'\r'}"
    case "${line}" in ''|'#'*) continue ;; esac
    case "${line}" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    key="$(printf '%s' "${key}" | tr -d '[:space:]')"
    [ -n "${key}" ] || continue
    case "${key}" in *[!A-Za-z0-9_]*) continue ;; esac
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    if [ -z "${!key:-}" ]; then
      export "${key}=${value}"
    fi
  done < "${file}"
}

# ---------------------------------------------------------------- 环境探测
compose_available() {
  [ -f "${COMPOSE_FILE}" ] || return 1
  command -v docker >/dev/null 2>&1 || return 1
  docker compose -f "${COMPOSE_FILE}" version >/dev/null 2>&1 || return 1
}

compose_db_running() {
  docker compose -f "${COMPOSE_FILE}" ps --status running --services 2>/dev/null | grep -qx 'db'
}

# ---------------------------------------------------------------- 前置检查
preflight() {
  command -v gzip >/dev/null 2>&1 || die "缺少 gzip 命令"
  command -v tar >/dev/null 2>&1 || die "缺少 tar 命令"
  command -v sha256sum >/dev/null 2>&1 || die "缺少 sha256sum 命令"

  case "${BACKUP_RETENTION_DAYS}" in
    ''|*[!0-9]*) die "BACKUP_RETENTION_DAYS 必须是正整数（当前：${BACKUP_RETENTION_DAYS}）" ;;
  esac
  [ "${BACKUP_RETENTION_DAYS}" -gt 0 ] || die "BACKUP_RETENTION_DAYS 必须大于 0（需要长期保留请设很大值，如 3650）"

  umask 077
  mkdir -p "${BACKUP_DIR}" 2>/dev/null \
    || die "无法创建备份目录 ${BACKUP_DIR}：用 BACKUP_DIR=<可写路径> 覆盖（本地开发建议 BACKUP_DIR=./backups）"
  chmod 700 "${BACKUP_DIR}" 2>/dev/null || true

  # 并发保护：同一时间只允许一个备份任务（cron 与手工恢复前的快照可能重叠）
  if command -v flock >/dev/null 2>&1; then
    exec 9>"${BACKUP_DIR}/.backup.lock"
    flock -n 9 || die "已有备份任务在执行（锁文件 ${BACKUP_DIR}/.backup.lock）"
  else
    log "警告：未找到 flock，跳过并发保护"
  fi
}

resolve_media_dir() {
  if [ -n "${HOST_MEDIA_DIR:-}" ]; then
    printf '%s' "${HOST_MEDIA_DIR}"
    return 0
  fi
  if [ -d /srv/ticket-system/media ]; then
    printf '%s' /srv/ticket-system/media
    return 0
  fi
  printf '%s' "${APP_DIR}/media"
}

cleanup_partials() {
  [ -n "${DB_DUMP}" ] && rm -f "${DB_DUMP}.part"
  [ -n "${MEDIA_TAR}" ] && rm -f "${MEDIA_TAR}.part"
  return 0
}

# ---------------------------------------------------------------- 数据库备份
verify_dump_tables() {
  local file="$1" found missing="" required
  found="$(gzip -dc "${file}" 2>/dev/null | grep -o 'CREATE TABLE `[^`]*`' | sed 's/CREATE TABLE `//; s/`$//' | sort -u || true)"
  [ -n "${found}" ] || die "备份文件里没有 CREATE TABLE 语句，导出不完整"
  for required in users mailboxes groups user_groups tickets messages attachments rules templates audit_logs settings django_migrations; do
    printf '%s\n' "${found}" | grep -qx "${required}" || missing="${missing} ${required}"
  done
  [ -z "${missing}" ] || die "备份缺少关键表：${missing}（数据库可能尚未迁移完成，请先执行 python manage.py migrate）"
  log "完整性校验通过：备份内含 $(printf '%s\n' "${found}" | wc -l | tr -d ' ') 张表"
}

dump_mariadb() {
  local tmp="${DB_DUMP}.part"
  local -a dump_cmd=()

  if compose_available && compose_db_running; then
    # 容器内执行：密码取自 db 容器自身的 MARIADB_* 环境变量，不出现在宿主机命令行/进程表里
    log "使用容器内 mariadb-dump（docker compose exec db）"
    dump_cmd=(docker compose -f "${COMPOSE_FILE}" exec -T db sh -c '
      MYSQL_PWD="${MARIADB_PASSWORD}" exec mariadb-dump \
        --single-transaction --skip-lock-tables --quick \
        --routines --triggers --events --hex-blob \
        --default-character-set=utf8mb4 \
        -u"${MARIADB_USER}" "${MARIADB_DATABASE}"')
  else
    local client host port
    client="$(command -v mariadb-dump || command -v mysqldump || true)"
    [ -n "${client}" ] || die "找不到备份途径：请使用 docker compose 部署，或安装 mariadb-client / mysql-client"
    [ -n "${DB_USER:-}" ] || die "DB_USER 未设置（宿主机客户端备份需要）"
    [ -n "${DB_NAME:-}" ] || die "DB_NAME 未设置（宿主机客户端备份需要）"
    [ -n "${DB_PASSWORD:-}" ] || die "DB_PASSWORD 未设置（宿主机客户端备份需要）"
    host="${BACKUP_DB_HOST:-127.0.0.1}"
    port="${BACKUP_DB_PORT:-3306}"
    log "使用宿主机客户端 ${client} 连接 ${host}:${port}（compose 默认不暴露 db 端口，此路径用于非容器化部署）"
    dump_cmd=(env "MYSQL_PWD=${DB_PASSWORD}" "${client}" -h"${host}" -P"${port}" \
      --single-transaction --skip-lock-tables --quick \
      --routines --triggers --events --hex-blob \
      --default-character-set=utf8mb4 \
      -u"${DB_USER}" "${DB_NAME}")
  fi

  if ! "${dump_cmd[@]}" | gzip -9 > "${tmp}"; then
    die "数据库逻辑备份失败：检查 db 容器状态，以及 DB_NAME / DB_USER / DB_PASSWORD 是否正确"
  fi
  [ -s "${tmp}" ] || die "数据库备份结果为空：数据库可能尚未初始化"
  gzip -t "${tmp}" || die "数据库备份文件不是有效的 gzip 流"
  verify_dump_tables "${tmp}"
  mv "${tmp}" "${DB_DUMP}"
  log "数据库备份完成：$(du -h "${DB_DUMP}" | cut -f1)  ${DB_DUMP}"
}

dump_sqlite() {
  local db_file="${APP_DIR}/db.sqlite3"
  local tmp="${DB_DUMP}.part"
  [ -f "${db_file}" ] || die "SQLite 数据库不存在：${db_file}"

  if command -v sqlite3 >/dev/null 2>&1; then
    log "使用 sqlite3 .backup 做在线一致性备份"
    sqlite3 "${db_file}" ".backup '${tmp}'" || die "sqlite3 .backup 失败"
  else
    log "警告：未安装 sqlite3 命令，退化为直接复制；有写入时可能不一致，建议先停止 runserver"
    cp -p "${db_file}" "${tmp}" || die "复制 SQLite 文件失败"
  fi

  [ -s "${tmp}" ] || die "SQLite 备份结果为空"
  gzip -9 -c "${tmp}" > "${DB_DUMP}" || die "gzip 压缩失败"
  rm -f "${tmp}"
  gzip -t "${DB_DUMP}" || die "SQLite 备份文件不是有效的 gzip 流"
  log "SQLite 备份完成：$(du -h "${DB_DUMP}" | cut -f1)  ${DB_DUMP}"
}

# ---------------------------------------------------------------- media 打包
archive_media() {
  local media_parent media_base tar_rc=0
  media_parent="$(dirname -- "${HOST_MEDIA_DIR}")"
  media_base="$(basename -- "${HOST_MEDIA_DIR}")"

  log "打包 media 目录：${HOST_MEDIA_DIR}（归档内根目录为 ${media_base}/）"
  set +e
  tar --warning=no-file-changed -czf "${MEDIA_TAR}.part" -C "${media_parent}" "${media_base}"
  tar_rc=$?
  set -e
  case "${tar_rc}" in
    0) : ;;
    1) log "警告：tar 返回 1（通常是有附件在打包过程中被写入/改名），归档已生成，继续" ;;
    *) die "media 打包失败（tar 退出码 ${tar_rc}）：检查 ${HOST_MEDIA_DIR} 读取权限与磁盘空间" ;;
  esac

  [ -s "${MEDIA_TAR}.part" ] || die "media 归档为空"
  gzip -t "${MEDIA_TAR}.part" || die "media 归档不是有效的 gzip 流"
  mv "${MEDIA_TAR}.part" "${MEDIA_TAR}"
  log "media 打包完成：$(du -h "${MEDIA_TAR}" | cut -f1)  ${MEDIA_TAR}"
}

# ---------------------------------------------------------------- 清单与保留
write_manifest() {
  local -a targets=("$(basename -- "${DB_DUMP}")")
  if [ -n "${MEDIA_TAR}" ]; then
    targets+=("$(basename -- "${MEDIA_TAR}")")
  fi
  ( cd "${BACKUP_DIR}" && sha256sum "${targets[@]}" > "$(basename -- "${MANIFEST}")" ) \
    || die "生成 SHA256 清单失败"
  chmod 600 "${MANIFEST}"
  log "校验清单：${MANIFEST}"
}

maybe_backup_env() {
  if [ "${BACKUP_INCLUDE_ENV}" != "1" ]; then
    log "未备份 .env：请确认 FERNET_KEY 已单独保存在密码管理器中（丢失则邮箱凭据无法解密）"
    return 0
  fi
  local target="${BACKUP_DIR}/ticket-env-${STAMP}${TAG_SUFFIX}.env.bak"
  [ -f "${APP_DIR}/.env" ] || { log "警告：${APP_DIR}/.env 不存在，跳过"; return 0; }
  cp -p "${APP_DIR}/.env" "${target}"
  chmod 600 "${target}"
  log "已备份 .env → ${target}（含 SECRET_KEY / FERNET_KEY，必须与数据库备份分开加密保管）"
}

prune_old_backups() {
  log "清理 ${BACKUP_RETENTION_DAYS} 天前的备份（匹配 ticket-*）"
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'ticket-*' \
    -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete || true
}

print_summary() {
  local -a files=("${DB_DUMP}" "${MANIFEST}")
  if [ -n "${MEDIA_TAR}" ]; then
    files+=("${MEDIA_TAR}")
  fi
  log "备份完成：时间戳 ${STAMP}${TAG_SUFFIX}"
  ls -lh "${files[@]}" || true
  log "恢复命令：scripts/restore.sh --stamp ${STAMP}${TAG_SUFFIX} --yes（演练步骤见 docs/部署手册.md §7）"
}

# ---------------------------------------------------------------- 主流程
main() {
  load_env_file "${APP_DIR}/.env"
  DB_ENGINE="${DB_ENGINE:-sqlite}"
  HOST_MEDIA_DIR="$(resolve_media_dir)"

  log "项目目录：${APP_DIR}"
  log "备份目录：${BACKUP_DIR}"

  preflight

  STAMP="$(date -u '+%Y%m%d-%H%M%S')"
  if [ -n "${BACKUP_TAG}" ]; then
    TAG_SUFFIX="-${BACKUP_TAG}"
  fi

  case "${DB_ENGINE}" in
    mysql|mariadb) DB_DUMP="${BACKUP_DIR}/ticket-db-${STAMP}${TAG_SUFFIX}.sql.gz" ;;
    sqlite)        DB_DUMP="${BACKUP_DIR}/ticket-db-${STAMP}${TAG_SUFFIX}.sqlite3.gz" ;;
    *)             die "不支持的 DB_ENGINE=${DB_ENGINE}（可选：sqlite | mysql | mariadb）" ;;
  esac
  MEDIA_TAR="${BACKUP_DIR}/ticket-media-${STAMP}${TAG_SUFFIX}.tar.gz"
  MANIFEST="${BACKUP_DIR}/ticket-${STAMP}${TAG_SUFFIX}.sha256"
  trap cleanup_partials EXIT

  local media_present=1
  if [ ! -d "${HOST_MEDIA_DIR}" ]; then
    media_present=0
    log "警告：media 目录不存在（${HOST_MEDIA_DIR}），本次跳过附件打包；可用 HOST_MEDIA_DIR=<路径> 指定"
  fi

  case "${DB_ENGINE}" in
    mysql|mariadb) dump_mariadb ;;
    sqlite)        dump_sqlite ;;
  esac

  if [ "${media_present}" = "1" ]; then
    archive_media
  else
    MEDIA_TAR=""
  fi

  write_manifest
  maybe_backup_env
  prune_old_backups
  print_summary
}

main "$@"
