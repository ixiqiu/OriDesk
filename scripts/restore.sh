#!/usr/bin/env bash
# ===========================================================================
# 新技元邮箱及工单管理系统 · 恢复脚本
#
#   从 scripts/backup.sh 产出的备份集恢复「数据库」与「media 附件目录」。
#
# 用法：
#   scripts/restore.sh --list                       列出可用备份集
#   scripts/restore.sh --latest --yes               恢复最新一套（全量）
#   scripts/restore.sh --stamp 20260218-023000 --yes
#   scripts/restore.sh --latest --db-only --yes     只恢复数据库
#   scripts/restore.sh --latest --media-only --yes
#
# 安全设计（为什么可以放心在生产执行）：
#   1) 先校验 SHA256 清单，校验不通过立即退出，绝不用损坏的备份覆盖生产数据；
#   2) 恢复前自动对现状做一次快照（BACKUP_TAG=pre-restore），恢复错了还能回去；
#   3) 恢复数据库前先停 web/worker/scheduler，避免恢复期间仍有进程读写信箱、发信；
#   4) media 采用「先解压到暂存目录，再原子替换」，旧目录保留为
#      <media>.before-restore-<时间戳>，不会被直接删除；
#   5) 默认需要手工输入 yes 确认，演练/自动化场景用 --yes。
#
# ⚠️ 恢复后必做（详见 docs/部署手册.md §7 备份恢复演练）
#   备份是「某个时间点的快照」：mailboxes.last_uid / uidvalidity 会被一并回退。
#   若恢复后直接放开调度器，IMAP 会把备份之后已经收过的邮件重新拉一遍，
#   造成重复工单与重复自动回复。因此脚本结束时会把「先冻结邮箱同步」的 SQL
#   打印出来，请核对 last_uid 后再逐个启用。
# ===========================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-${APP_DIR}/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-/srv/ticket-system/backups}"

STAMP_ARG=""
MODE="all"          # all | db | media
ASSUME_YES=0
LIST_ONLY=0

STAMP=""
DB_DUMP=""
MEDIA_TAR=""
MANIFEST=""

log() { printf '[restore][%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { printf '[restore][%s] ERROR: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：scripts/restore.sh [选项]

  --list, -l           列出备份目录中可用的备份集
  --latest             使用最新一套备份（默认行为）
  --stamp <时间戳>     指定备份时间戳，如 20260218-023000
                       （带 -pre-restore 后缀的恢复前快照同样可用）
  --db-only            只恢复数据库
  --media-only         只恢复 media 附件目录
  --yes, -y            跳过交互确认（用于演练 / 自动化）
  -h, --help           显示本帮助

相关环境变量：
  APP_DIR（项目目录） BACKUP_DIR（备份目录） COMPOSE_FILE HOST_MEDIA_DIR DB_ENGINE
  SKIP_SAFETY_BACKUP=1   跳过「恢复前现状快照」

示例：
  BACKUP_DIR=./backups scripts/restore.sh --list
  scripts/restore.sh --latest --yes
EOF
}

# ---------------------------------------------------------------- .env 解析
# 与 config/settings.py::_load_dotenv 语义一致：不 eval，已存在的环境变量优先。
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

compose_available() {
  [ -f "${COMPOSE_FILE}" ] || return 1
  command -v docker >/dev/null 2>&1 || return 1
  docker compose -f "${COMPOSE_FILE}" version >/dev/null 2>&1 || return 1
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

# ---------------------------------------------------------------- 参数解析
while [ "$#" -gt 0 ]; do
  case "$1" in
    --stamp)
      [ "$#" -ge 2 ] || die "--stamp 需要一个时间戳参数"
      STAMP_ARG="$2"
      shift 2
      ;;
    --stamp=*)  STAMP_ARG="${1#*=}"; shift ;;
    --latest)   STAMP_ARG=""; shift ;;
    --db-only)  MODE="db"; shift ;;
    --media-only) MODE="media"; shift ;;
    --yes|-y)   ASSUME_YES=1; shift ;;
    --list|-l)  LIST_ONLY=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          die "未知参数：$1（用 --help 查看用法）" ;;
  esac
done

strip_db_suffix() {
  local name="$1"
  name="${name#ticket-db-}"
  name="${name%.sql.gz}"
  name="${name%.sqlite3.gz}"
  printf '%s' "${name}"
}

# ---------------------------------------------------------------- 备份集枚举
list_backups() {
  local found=0 file stamp kind media_flag manifest_flag size
  [ -d "${BACKUP_DIR}" ] || die "备份目录不存在：${BACKUP_DIR}（用 BACKUP_DIR=<路径> 指定）"
  log "备份目录：${BACKUP_DIR}"
  printf '%-24s %-10s %-7s %-8s %s\n' 'STAMP(UTC)' 'DB' 'MEDIA' 'SHA256' 'SIZE'
  while IFS= read -r file; do
    [ -n "${file}" ] || continue
    found=1
    stamp="$(strip_db_suffix "$(basename -- "${file}")")"
    case "${file}" in
      *.sqlite3.gz) kind="sqlite3" ;;
      *)            kind="sql.gz" ;;
    esac
    if [ -f "${BACKUP_DIR}/ticket-media-${stamp}.tar.gz" ]; then media_flag="yes"; else media_flag="no"; fi
    if [ -f "${BACKUP_DIR}/ticket-${stamp}.sha256" ]; then manifest_flag="yes"; else manifest_flag="no"; fi
    size="$(du -h "${file}" | cut -f1)"
    printf '%-24s %-10s %-7s %-8s %s\n' "${stamp}" "${kind}" "${media_flag}" "${manifest_flag}" "${size}"
  done < <(find "${BACKUP_DIR}" -maxdepth 1 -type f \( -name 'ticket-db-*.sql.gz' -o -name 'ticket-db-*.sqlite3.gz' \) | sort)
  [ "${found}" = "1" ] || log "备份目录 ${BACKUP_DIR} 中没有找到任何备份集"
}

resolve_stamp() {
  if [ -n "${STAMP_ARG}" ]; then
    STAMP="${STAMP_ARG}"
  else
    local latest
    latest="$(find "${BACKUP_DIR}" -maxdepth 1 -type f \( -name 'ticket-db-*.sql.gz' -o -name 'ticket-db-*.sqlite3.gz' \) | sort | tail -n 1 || true)"
    [ -n "${latest}" ] || die "在 ${BACKUP_DIR} 中找不到任何备份集（用 --list 查看）"
    STAMP="$(strip_db_suffix "$(basename -- "${latest}")")"
    log "未指定 --stamp，使用最新备份集：${STAMP}"
  fi

  DB_DUMP=""
  local candidate
  for candidate in "${BACKUP_DIR}/ticket-db-${STAMP}.sql.gz" "${BACKUP_DIR}/ticket-db-${STAMP}.sqlite3.gz"; do
    if [ -f "${candidate}" ]; then DB_DUMP="${candidate}"; break; fi
  done
  [ -n "${DB_DUMP}" ] || die "找不到数据库备份：${BACKUP_DIR}/ticket-db-${STAMP}.sql.gz（或 .sqlite3.gz）"

  MEDIA_TAR="${BACKUP_DIR}/ticket-media-${STAMP}.tar.gz"
  [ -f "${MEDIA_TAR}" ] || MEDIA_TAR=""
  MANIFEST="${BACKUP_DIR}/ticket-${STAMP}.sha256"
  [ -f "${MANIFEST}" ] || MANIFEST=""
}

# ---------------------------------------------------------------- 校验与确认
verify_backup() {
  if [ -n "${MANIFEST}" ]; then
    log "校验 SHA256 清单：$(basename -- "${MANIFEST}")"
    ( cd "${BACKUP_DIR}" && sha256sum -c "$(basename -- "${MANIFEST}")" ) \
      || die "校验失败：备份文件已损坏或被改动，拒绝恢复"
  else
    log "警告：未找到校验清单 ticket-${STAMP}.sha256，跳过完整性校验"
  fi

  if [ "${MODE}" != "media" ]; then
    gzip -t "${DB_DUMP}" || die "数据库备份不是有效的 gzip 流：${DB_DUMP}"
  fi
  if [ "${MODE}" != "db" ]; then
    [ -n "${MEDIA_TAR}" ] || die "找不到附件归档 ${BACKUP_DIR}/ticket-media-${STAMP}.tar.gz（只恢复数据库请加 --db-only）"
    gzip -t "${MEDIA_TAR}" || die "附件归档不是有效的 gzip 流：${MEDIA_TAR}"
  fi
}

confirm() {
  if [ "${ASSUME_YES}" = "1" ]; then
    log "--yes：跳过交互确认"
    return 0
  fi
  local db_label media_label answer=""
  if [ "${MODE}" = "media" ]; then db_label="（本次跳过）"; else db_label="${DB_DUMP}"; fi
  if [ "${MODE}" = "db" ]; then media_label="（本次跳过）"; else media_label="${MEDIA_TAR:-（无附件归档）}"; fi

  printf '\n即将执行【不可逆】恢复操作：\n'
  printf '  备份时间戳   : %s\n' "${STAMP}"
  printf '  数据库备份   : %s\n' "${db_label}"
  printf '  附件归档     : %s\n' "${media_label}"
  printf '  目标数据库   : %s @ %s\n' "${DB_NAME:-ticket_system}" "${DB_HOST:-db}"
  printf '  目标附件目录 : %s（现有目录会改名保留，不会直接删除）\n' "${HOST_MEDIA_DIR}"
  printf '  服务影响     : web / worker / scheduler 会被停止，恢复后再启动\n'
  printf '输入 yes 继续，其他任何输入都会中止：'
  read -r answer || true
  [ "${answer}" = "yes" ] || die "已取消恢复"
}

safety_backup() {
  if [ "${SKIP_SAFETY_BACKUP:-0}" = "1" ]; then
    log "SKIP_SAFETY_BACKUP=1：跳过恢复前现状快照"
    return 0
  fi
  log "恢复前先对现状做一次快照（BACKUP_TAG=pre-restore），失败则中止恢复"
  APP_DIR="${APP_DIR}" \
  BACKUP_DIR="${BACKUP_DIR}" \
  COMPOSE_FILE="${COMPOSE_FILE}" \
  HOST_MEDIA_DIR="${HOST_MEDIA_DIR}" \
  DB_ENGINE="${DB_ENGINE:-}" \
  BACKUP_INCLUDE_ENV="${BACKUP_INCLUDE_ENV:-0}" \
  BACKUP_TAG="pre-restore" \
    bash "${SCRIPT_DIR}/backup.sh" \
    || die "现状快照失败，恢复已中止（确认无需快照可用 SKIP_SAFETY_BACKUP=1 跳过）"
}

# ---------------------------------------------------------------- 服务控制
stop_app_services() {
  compose_available || die "需要可用的 docker compose 才能执行恢复"
  log "停止应用服务（保留 db / redis 运行）"
  docker compose -f "${COMPOSE_FILE}" stop web worker scheduler
}

start_app_services() {
  log "启动应用服务（web 容器入口会自动执行 migrate）"
  docker compose -f "${COMPOSE_FILE}" up -d web worker scheduler
}

# ---------------------------------------------------------------- 数据库恢复
restore_mariadb() {
  compose_available || die "需要可用的 docker compose 才能恢复 MariaDB"

  log "确保障碍数据库存在且字符集为 utf8mb4：${DB_NAME:-ticket_system}"
  docker compose -f "${COMPOSE_FILE}" exec -T db sh -c '
    MYSQL_PWD="${MARIADB_ROOT_PASSWORD}" exec mariadb --default-character-set=utf8mb4 -uroot -e \
      "CREATE DATABASE IF NOT EXISTS \`${MARIADB_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"' \
    || die "创建/检查目标数据库失败（检查 DB_ROOT_PASSWORD）"

  log "导入数据库备份。恢复期间关闭外键/唯一键检查：备份文件里是 DROP+CREATE 的整表重建"
  if ! gzip -dc "${DB_DUMP}" | docker compose -f "${COMPOSE_FILE}" exec -T db sh -c '
      MYSQL_PWD="${MARIADB_PASSWORD}" exec mariadb --default-character-set=utf8mb4 \
        --init-command="SET FOREIGN_KEY_CHECKS=0; SET UNIQUE_CHECKS=0;" \
        -u"${MARIADB_USER}" "${MARIADB_DATABASE}"'; then
    die "数据库导入失败：检查备份完整性，以及 DB_USER 是否具备该库的建表权限"
  fi

  local tables
  tables="$(docker compose -f "${COMPOSE_FILE}" exec -T db sh -c '
    MYSQL_PWD="${MARIADB_PASSWORD}" mariadb -N -B --default-character-set=utf8mb4 \
      -u"${MARIADB_USER}" "${MARIADB_DATABASE}" \
      -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE()"' | tr -d '\r')"
  [ -n "${tables}" ] || die "无法确认恢复结果：查询表数量失败"
  log "数据库恢复完成，当前表数量：${tables}"
}

restore_sqlite() {
  local db_file="${APP_DIR}/db.sqlite3"
  local safety="${db_file}.before-restore-$(date -u '+%Y%m%d-%H%M%S')"
  if [ -f "${db_file}" ]; then
    cp -p "${db_file}" "${safety}"
    log "原 SQLite 文件已保留：${safety}"
  fi
  gzip -dc "${DB_DUMP}" > "${db_file}.part" || die "解压 SQLite 备份失败"
  mv "${db_file}.part" "${db_file}"
  log "SQLite 恢复完成：${db_file}"
}

# ---------------------------------------------------------------- media 恢复
restore_media() {
  local parent base staging old
  parent="$(dirname -- "${HOST_MEDIA_DIR}")"
  base="$(basename -- "${HOST_MEDIA_DIR}")"
  staging="${HOST_MEDIA_DIR}.restore-${STAMP}"
  old="${HOST_MEDIA_DIR}.before-restore-$(date -u '+%Y%m%d-%H%M%S')"

  [ -d "${parent}" ] || die "父目录不存在：${parent}"
  rm -rf "${staging}"
  mkdir -p "${staging}"

  log "解压附件归档到暂存目录：${staging}"
  tar -xzf "${MEDIA_TAR}" -C "${staging}" || die "解压附件归档失败"
  [ -d "${staging}/${base}" ] || die "归档结构异常：未找到 ${staging}/${base}（归档应包含 ${base}/ 根目录）"

  if [ -e "${HOST_MEDIA_DIR}" ]; then
    mv "${HOST_MEDIA_DIR}" "${old}"
    log "原附件目录已保留为：${old}（核对无误后可手工删除以释放空间）"
  fi
  mv "${staging}/${base}" "${HOST_MEDIA_DIR}"
  rmdir "${staging}" 2>/dev/null || true

  # 容器内进程以 uid/gid 10001 运行，必须能写 media（附件落盘）
  if [ "$(id -u)" -eq 0 ]; then
    chown -R 10001:10001 "${HOST_MEDIA_DIR}"
    log "已把 ${HOST_MEDIA_DIR} 属主设为 10001:10001"
  else
    log "当前非 root：请确认 ${HOST_MEDIA_DIR} 属主为 10001:10001（sudo chown -R 10001:10001 ${HOST_MEDIA_DIR}）"
  fi

  local count
  count="$(find "${HOST_MEDIA_DIR}" -type f 2>/dev/null | wc -l | tr -d ' ')"
  log "附件目录恢复完成，文件数：${count}  路径：${HOST_MEDIA_DIR}"
}

# ---------------------------------------------------------------- 恢复后检查
post_restore_check() {
  if [ "${DB_ENGINE}" = "sqlite" ]; then
    log "SQLite 模式：无需容器健康检查，请自行重启 runserver / rqworker"
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    log "未找到 curl，跳过 /healthz 探测；请手工执行 curl -fsS http://127.0.0.1:${WEB_PORT:-8000}/healthz"
    return 0
  fi

  local waited=0
  while [ "${waited}" -lt 150 ]; do
    if curl -fsS "http://127.0.0.1:${WEB_PORT:-8000}/healthz" >/dev/null 2>&1; then
      log "/healthz 返回 200，Web 已就绪"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  log "警告：/healthz 在 150s 内未返回 200，请执行 docker compose logs --tail=100 web 排查"
}

print_next_steps() {
  cat <<EOF

────────────────────────────────────────────────────────────────────────────
恢复完成，请按顺序完成以下核对（详见 docs/部署手册.md §7）：

1) 冻结 IMAP 同步，防止用旧的 last_uid 重复拉取邮件（产生重复工单）：

   docker compose exec db sh -c 'MYSQL_PWD="\$MARIADB_PASSWORD" mariadb \\
     -u"\$MARIADB_USER" "\$MARIADB_DATABASE" -e "UPDATE mailboxes SET is_active = 0"'

   docker compose exec db sh -c 'MYSQL_PWD="\$MARIADB_PASSWORD" mariadb \\
     -u"\$MARIADB_USER" "\$MARIADB_DATABASE" \\
     -e "SELECT id, email, last_uid, uidvalidity, is_active FROM mailboxes"'

2) 核对每个邮箱的 last_uid / uidvalidity 与邮件服务端现状是否一致
   （不一致时置为服务端当前 UIDNEXT-1 并同步 uidvalidity），再置 is_active = 1。

3) 抽样验证：
   - 工单列表能打开、最新工单时间符合预期；
   - 附件能下载（随附件目录一起恢复）；
   - 发一封测试邮件确认收发链路可用。

4) 确认无误后，方可删除 <media>.before-restore-* 与 pre-restore 快照。
────────────────────────────────────────────────────────────────────────────
EOF
}

# ---------------------------------------------------------------- 主流程
main() {
  load_env_file "${APP_DIR}/.env"
  DB_ENGINE="${DB_ENGINE:-sqlite}"
  HOST_MEDIA_DIR="$(resolve_media_dir)"

  if [ "${LIST_ONLY}" = "1" ]; then
    list_backups
    return 0
  fi

  [ -d "${BACKUP_DIR}" ] || die "备份目录不存在：${BACKUP_DIR}（用 BACKUP_DIR=<路径> 指定）"
  resolve_stamp
  verify_backup
  confirm

  if [ "${DB_ENGINE}" = "sqlite" ]; then
    log "DB_ENGINE=sqlite：本地开发模式，请确认已停止 runserver / rqworker 等写入进程"
  else
    stop_app_services
  fi

  safety_backup

  if [ "${MODE}" != "media" ]; then
    case "${DB_ENGINE}" in
      mysql|mariadb) restore_mariadb ;;
      sqlite)        restore_sqlite ;;
      *)             die "不支持的 DB_ENGINE=${DB_ENGINE}（可选：sqlite | mysql | mariadb）" ;;
    esac
  fi

  if [ "${MODE}" != "db" ]; then
    restore_media
  fi

  if [ "${DB_ENGINE}" = "sqlite" ]; then
    post_restore_check
  else
    start_app_services
    post_restore_check
  fi

  log "恢复流程结束（时间戳 ${STAMP}）"
  print_next_steps
}

main "$@"
