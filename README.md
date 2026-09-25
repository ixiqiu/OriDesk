# 新技元邮箱及工单管理系统（OriDesk）

一套自部署的多用户共享邮箱与工单系统：连接飞书企业邮箱的 IMAP/SMTP，把统一邮箱、各用户组专用邮箱、全局兜底邮箱的来信转成工单，按主题路由到用户组，支持组内协作、组身份发信、自动回复、改派与全链路审计。

- 开发依据：[开发文档 v1.1](docs/新技元邮箱及工单管理系统-开发文档-v1.1.md)（技术栈 §8 / 部署架构 §9 / 安全清单 §10 / MVP 路线 §11）
- 部署手册：[docs/部署手册.md](docs/部署手册.md)
- 运维手册：[docs/运维手册.md](docs/运维手册.md)

> **就绪门槛**：模型、服务层、邮件处理流水线（防循环、HTML 净化、附件解析、IMAP 增量同步、路由/粘性/自动回复）、Web 视图与模板、APScheduler 定时任务均已落地。部署前必须通过两道关卡——`python manage.py check` 无错误、`python -m pytest` 全绿（CI 还会额外跑 `makemigrations --check --dry-run`、`check --deploy` 与 MariaDB 集成测试，见 `.github/workflows/ci.yml`）。

---

## 1. 功能清单

| # | 功能 | 关键行为 | 代码落点 |
|---|---|---|---|
| 1 | 多层级邮箱入口 | 统一邮箱 / 组专用邮箱 / 全局兜底邮箱（全局唯一）/ 管理员组邮箱 | `apps/accounts/models.py`（`Mailbox`、`Group`） |
| 2 | 邮件接入 | IMAP 增量拉取，按 `UIDVALIDITY` 变更重置同步位点，`last_uid` 只推进到连续成功处理的最后一封 | `apps/mailboxes/sync.py` |
| 3 | 防循环过滤 | `Auto-Submitted`、`X-Auto-Response-Suppress`、`Precedence: bulk`、`List-Id`、来自本系统邮箱、`no-reply@`/`mailer-daemon@` | `apps/mailboxes/filters.py` |
| 4 | HTML 净化 | bleach 白名单标签/属性，远程图片默认不加载 | `apps/mailboxes/sanitizer.py` |
| 5 | 附件处理 | 落盘 `media/attachments/{ticket_id}/{message_key}/{filename}`，危险扩展名仅可下载 | `apps/mailboxes/storage.py`、`apps/tickets/models.py` |
| 6 | 工单归并 | `References`/`In-Reply-To` 优先，其次主题 `[T#123]`；两者都必须校验发件人与工单 `customer_email` 一致 | `apps/tickets/services.py::find_ticket` |
| 7 | 幂等保护 | 同一入口邮箱的同一 `Message-ID` 只处理一次（防 IMAP 重投、UIDVALIDITY 重扫导致的重复工单） | `apps/mailboxes/pipeline.py::already_seen` |
| 8 | 主题路由 | 短期粘性（`last_message_at` + `sticky_window_days`）→ 规则按优先级首个命中 → 兜底组 → 管理员组 | `apps/routing/services.py` |
| 9 | 自动回复 | 首次进线判断（`first_contact_window_hours`）+ 防重复，模板"组覆盖 > 全局"，回信带 `Auto-Submitted: auto-replied` | `apps/autoresponder/services.py` |
| 10 | 组身份发信 | 对外 `From`/`Reply-To` 用组邮箱，无邮箱组走全局兜底邮箱，内部记录 `actual_sender` | `apps/mailboxes/services.py` |
| 11 | 待回复标签与认领 | 客户来信置 `is_awaiting_reply=True`，外发回复置回 `False`；认领可选 | `apps/tickets/services.py` |
| 12 | 可见性与改派 | 超管/组管理员/管理员组成员跨组可见，普通用户仅本组；改派写审计 | `apps/tickets/selectors.py`、`apps/routing/` |
| 13 | 审计日志 | 回复、改派、认领、配置变更、登录等动作全量留痕 | `apps/audit/models.py`、`apps/audit/services.py` |
| 14 | 系统设置 | 键值配置：粘性窗口、首次进线窗口、兜底组/邮箱、附件上限、轮询间隔 | `apps/audit/models.py::Setting`（默认值见 §4.6） |
| 15 | 健康检查 | `GET /healthz`，200=正常 / 503=数据库不可达 | `apps/core/views.py::healthz` |
| 16 | 定时同步 | APScheduler 按 `imap_poll_interval_seconds` 轮询所有启用邮箱 | `python manage.py runapscheduler` |
| 17 | 异步队列 | RQ 队列 `default` / `mail`，Redis 不可用时自动降级为同步执行并告警 | `apps/mailboxes/tasks.py::dispatch` |

### 环境变量映射的关键配置项

数据库中的 `settings` 表可运行时覆盖以下默认值（开发文档 §4.6）：

| key | 默认 | 说明 |
|---|---|---|
| `sticky_window_days` | 7 | 短期粘性天数，基准是工单 `last_message_at` |
| `first_contact_window_hours` | 24 | 首次进线判断窗口 |
| `fallback_group_id` | 空 | 兜底组；为空则进管理员组 |
| `fallback_mailbox_id` | 空 | 全局兜底邮箱；为空则用 `is_fallback=True` 的邮箱 |
| `max_attachment_size_mb` | 25 | 附件大小上限（Nginx 侧 `client_max_body_size` 为 30M） |
| `imap_poll_interval_seconds` | 60 | IMAP 轮询间隔，可被环境变量覆盖（环境变量优先） |

---

## 2. 技术栈

选型与开发文档 §8 一致，版本为当前 `requirements.txt` 在本机解析到的实际版本：

| 模块 | 选型 | 本项目实际版本 |
|---|---|---|
| 语言 | Python 3.12 | 3.12 |
| Web 框架 | Django 5.x | Django 5.2.17 |
| 前端 | Django 模板 + HTMX + Tailwind | 模板目录 `templates/`，静态源 `static/` |
| 数据库 | MariaDB 11（utf8mb4） | 生产 `mariadb:11` 容器；本地/单测 SQLite |
| 数据库驱动 | PyMySQL（`mysqlclient` 不可用时回退） | PyMySQL 1.2.3（注册见 `config/__init__.py`） |
| 队列 | RQ + Redis | django-rq 4.2.0 / rq 2.12.0 / redis 8.1.0 |
| 定时任务 | APScheduler | 3.11.3 |
| IMAP / SMTP | imapclient / smtplib | imapclient 4.1.0 |
| HTML 净化 | bleach | 6.4.0 |
| 密码哈希 | Argon2 | argon2-cffi 25.1.0 |
| 凭据加密 | cryptography Fernet | 50.0.1 |
| WSGI | gunicorn | 26.2.0 |
| 部署 | Docker Compose + 现有 Nginx | 见 `Dockerfile`、`docker-compose.yml`、`deploy/nginx/` |
| 测试 | pytest + pytest-django | pytest 9.1.1 / pytest-django 4.14.0 |

---

## 3. 快速开始

### 3.1 路径 A：本地 SQLite（最快的跑通方式）

本地开发不需要 MariaDB、Redis、Docker：`config/settings.py` 在 `DB_ENGINE=sqlite` 时自动使用 `db.sqlite3`，队列在 Redis 不可用时自动同步执行。

```bash
cd /data/dsh/home/OriDesk

# 1) 准备虚拟环境（本仓库已带 .venv，可跳过本步）
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
#    本工作区已用 uv 建好 .venv（里面没有 pip），用 uv 的等价写法：
#      ./.toolchain/uv venv --python 3.12                                    # 从零创建
#      ./.toolchain/uv pip install -r requirements-dev.txt --python .venv/bin/python

# 2) 准备 .env（settings.py 会自己读取项目根目录的 .env，无需手动 export）
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，本地开发至少要改这四项：

```env
# 本地没有 MariaDB：切到 SQLite
DB_ENGINE=sqlite
# 生产必须为 True；本地调试建议 False，便于看完整报错
DJANGO_DEBUG=True
# 本地没有 HTTPS。置为 False，否则 http://127.0.0.1:8000 会被 301 到 https 打不开
DJANGO_SECURE_SSL_REDIRECT=False
# 生成两个真实密钥（不要沿用示例文本）
# DJANGO_SECRET_KEY=<openssl rand -base64 48 的输出>
# FERNET_KEY=<openssl rand -base64 32 | tr '+/' '-_' 的输出>
```

```bash
# 3) 建表 + 建管理员账号 + 起服务
python manage.py migrate
python manage.py createsuperuser      # 之后可在 /admin/ 里建用户组、绑定邮箱
python manage.py runserver 127.0.0.1:8000

# 4) 验证
curl -fsS http://127.0.0.1:8000/healthz
# {"status": "ok", "database": true}
```

打开 <http://127.0.0.1:8000/> 进入收件箱，<http://127.0.0.1:8000/admin/> 进入管理后台。
邮箱授权码加密依赖 `FERNET_KEY`，**同一个密钥必须长期保留**：换掉它，库里的邮箱凭据就全部解不开（见 [FAQ](#12-faq)）。

### 3.2 路径 B：Docker Compose（与生产同构）

前提：宿主机已装 Docker + Compose v2，并已有 Nginx。完整步骤见 [docs/部署手册.md](docs/部署手册.md) §2，速览：

```bash
cd /data/dsh/home/OriDesk

# 1) 宿主机目录（容器内以 uid/gid 10001 运行，必须提前授权）
sudo mkdir -p /srv/ticket-system/{media,staticfiles,backups}
sudo chown -R 10001:10001 /srv/ticket-system/media /srv/ticket-system/staticfiles

# 2) 生成生产 .env（三个密钥一律现场生成，绝不沿用示例值）
cp .env.example .env && chmod 600 .env
sed -i "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$(openssl rand -base64 48)|" .env
sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')|" .env
sed -i "s|^DB_ROOT_PASSWORD=.*|DB_ROOT_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=')|" .env
sed -i "s|^FERNET_KEY=.*|FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')|" .env
# 再确认：DB_ENGINE=mysql、DB_HOST=db、DJANGO_ALLOWED_HOSTS=你的域名、
#         DJANGO_CSRF_TRUSTED_ORIGINS=https://你的域名、DJANGO_DEBUG=False

# 3) 启动五个服务（web / worker / scheduler / db / redis）
docker compose up -d --build
docker compose ps                     # db/redis/web 应为 healthy
curl -fsS http://127.0.0.1:8000/healthz

# 4) 建管理员账号（在已运行的 web 容器里执行）
docker compose exec web python manage.py createsuperuser
```

容器内的 `web` 入口会自动执行 `migrate` → `collectstatic`，无需手工介入；`worker` 与 `scheduler` 不执行迁移，避免并发迁移互相等锁。

### 3.3 接上 Nginx 与 HTTPS

```bash
sudo cp deploy/nginx/ticket.example.com.conf /etc/nginx/sites-available/ticket.example.com.conf
sudo sed -i 's/ticket\.example\.com/你的域名/g' /etc/nginx/sites-available/ticket.example.com.conf
sudo ln -s /etc/nginx/sites-available/ticket.example.com.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

证书路径/域名/静态目录的对应关系、certbot 首次签发顺序见 [docs/部署手册.md](docs/部署手册.md) §4、§5。

### 3.4 初始化数据

系统没有"一键初始化"脚本，按下面顺序在管理后台（`/admin/`）录入即可：

1. **邮箱**（`Mailbox`）：填 IMAP/SMTP 主机、端口、账号、授权码；授权码通过 `set_secret()` 加密存储。指定唯一的全局兜底邮箱（`is_fallback`）。
2. **用户组**（`Group`）：建组并绑定对外邮箱；需要跨组可见的组勾选 `is_admin_group`。
3. **用户与组成员**（`UserGroup`）：分配成员，组管理员在原组内勾 `is_admin`。
4. **路由规则**（`Rule`）：按入口邮箱 + 优先级配置主题/发件人/收件人/正文匹配与动作。
5. **自动回复模板**（`Template`）：全局一套，组可按需覆盖。

---

## 4. 目录结构

```text
OriDesk/
├── config/                     # Django 项目包
│   ├── settings.py             # 单文件配置：.env 加载、SQLite/MariaDB 切换、安全开关、日志
│   ├── urls.py                 # URL 总入口（/admin/ /accounts/ /routing/ /audit/ /healthz …）
│   ├── wsgi.py                 # WSGI 入口：config.wsgi:application
│   └── asgi.py
├── apps/
│   ├── accounts/               # User / Mailbox / Group / UserGroup
│   ├── mailboxes/              # IMAP 同步、MIME 解析、防循环、HTML 净化、附件存储、RQ 任务
│   ├── tickets/                # Ticket / Message / Attachment + 归并、认领、待回复、可见性
│   ├── routing/                # Rule / Template + 路由引擎、粘性、兜底、改派
│   ├── autoresponder/          # 自动回复：首次进线判断、模板渲染、防重复
│   ├── audit/                  # AuditLog / Setting + 审计服务与中间件
│   └── core/                   # 健康检查、错误页、Fernet 加解密、公共工具、管理命令
│       └── management/commands/runapscheduler.py   # APScheduler 定时任务入口
├── templates/  static/         # 模板与静态源文件（collectstatic 后进 staticfiles/）
├── deploy/nginx/ticket.example.com.conf            # 宿主 Nginx 站点配置
├── docker/entrypoint.sh        # 容器入口：等待 DB/Redis → migrate → collectstatic → 启动
├── scripts/backup.sh           # MariaDB 逻辑备份 + media 打包 + 保留 N 天
├── scripts/restore.sh          # 校验 → 快照 → 停止服务 → 恢复 → 启动 → 核对清单
├── docs/                       # 部署手册、运维手册
├── Dockerfile                  # python:3.12-slim 多阶段、非 root(10001)、含 HEALTHCHECK
├── docker-compose.yml          # web / worker / scheduler / db / redis
├── .dockerignore               # 排除 .env、.venv、db.sqlite3、media、staticfiles
├── .env.example                # 环境变量模板（.env 不入库）
├── requirements.txt            # 运行时依赖（勿在部署产物中改动）
├── requirements-dev.txt        # 运行时 + pytest 系列
└── pytest.ini                  # DJANGO_SETTINGS_MODULE=config.settings，testpaths=apps
```

部署后的运行期目录（宿主）：

```text
/srv/ticket-system/
├── media/           # MEDIA_ROOT 挂载点：attachments/{ticket_id}/{message_key}/{filename}
├── staticfiles/     # STATIC_ROOT 挂载点：collectstatic 产物，Nginx 直出
└── backups/         # scripts/backup.sh 输出（权限 700，含客户个人信息）
```

---

## 5. 常用命令

### 本地开发

| 目的 | 命令 |
|---|---|
| 配置自检 | `python manage.py check` |
| 生成并检查迁移 | `python manage.py makemigrations --check --dry-run` |
| 查看迁移状态 | `python manage.py showmigrations` |
| 应用迁移 | `python manage.py migrate` |
| 建管理员 | `python manage.py createsuperuser` |
| 起开发服务器 | `python manage.py runserver 127.0.0.1:8000` |
| 交互式调试 | `python manage.py shell` |
| 启动队列 worker | `python manage.py rqworker default mail` |
| 启动定时同步 | `python manage.py runapscheduler` |

### 容器运维

| 目的 | 命令 |
|---|---|
| 构建并启动 | `docker compose up -d --build` |
| 查看状态 | `docker compose ps` |
| 跟踪日志 | `docker compose logs -f --tail=200 web worker scheduler` |
| 只看某个服务 | `docker compose logs -f --tail=200 scheduler` |
| 重启单个服务 | `docker compose restart worker` |
| 进入容器 | `docker compose exec web bash` |
| 容器内建管理员 | `docker compose exec web python manage.py createsuperuser` |
| 手工收集静态文件 | `docker compose exec web python manage.py collectstatic --noinput` |
| 健康检查 | `curl -fsS http://127.0.0.1:8000/healthz` |
| 停止全部 | `docker compose down`（**不要**加 `-v`，会删掉数据库卷） |

### 备份与恢复

| 目的 | 命令 |
|---|---|
| 立即备份 | `sudo /data/dsh/home/OriDesk/scripts/backup.sh` |
| 列出备份集 | `scripts/restore.sh --list` |
| 从最新备份恢复 | `sudo scripts/restore.sh --latest --yes` |
| 只恢复数据库 | `sudo scripts/restore.sh --latest --db-only --yes` |
| 本地演练（SQLite） | `BACKUP_DIR=./backups scripts/backup.sh` |

备份脚本的定时任务与恢复后必做的核对步骤见 [docs/部署手册.md](docs/部署手册.md) §7。

---

## 6. 测试

```bash
# 全量测试（pytest.ini 已配置 DJANGO_SETTINGS_MODULE 与 testpaths=apps tests）
python -m pytest

# 只跑某些目录，并显示用例名
python -m pytest tests/test_merge.py apps/tickets -v

# 覆盖率（与 CI 一致）
python -m pytest --cov=apps --cov-report=term-missing

# 集成测试走 MariaDB：用 root + 独立测试库，Django 会自动建/删 test_<DB_NAME>
# 注意：变量只在这一条命令内生效，不要把它写成容器级环境变量
docker compose exec web sh -c 'DB_USER=root DB_PASSWORD="$DB_ROOT_PASSWORD" DB_NAME=ticket_system_test python -m pytest -q'
```

测试用例分布在两处：仓库根 `tests/`（归并、路由、可见性、发信、审计、UI 冒烟等跨模块用例）与 `apps/<app>/tests/`（各模块自身用例）；`pytest.ini` 的 `testpaths = apps tests` 会同时收集。单测默认跑内存 SQLite（`config/settings.py` 在测试模式下使用 `:memory:`），CI 另有 MariaDB 集成任务验证 utf8mb4、索引长度与 JSON 字段。

---

## 7. 部署与运维文档

| 文档 | 内容 |
|---|---|
| [docs/部署手册.md](docs/部署手册.md) | 首次部署步骤、环境变量逐项说明、Nginx 配置说明、证书签发、升级/回滚、备份恢复演练、监控与日志、故障排查表 |
| [docs/运维手册.md](docs/运维手册.md) | 日常巡检（IMAP 同步/队列积压/磁盘/附件目录）、常见故障处置（授权码失效、UIDVALIDITY 变化、邮件风暴、worker 掉线）、应急处置流程 |
| `deploy/nginx/ticket.example.com.conf` | HTTPS 站点配置，含 `/media/` 匿名下载风险说明与整改建议 |
| `scripts/backup.sh` / `scripts/restore.sh` | 备份与恢复，含 SHA256 校验与恢复后核对清单 |

---

## 8. 环境变量速查

完整逐项说明见 [docs/部署手册.md](docs/部署手册.md) §3。示例值一律用 `CHANGE_ME` 或 `$(openssl rand ...)` 表示，`.env` 永不入库（已在 `.gitignore` 与 `.dockerignore` 中排除）。

| 变量 | 必填 | 示例 | 说明 |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | 是 | `$(openssl rand -base64 48)` | Django 密钥；缺失时非 DEBUG 环境直接拒绝启动 |
| `DJANGO_DEBUG` | 是 | `False` | 生产必须为 `False` |
| `DJANGO_ALLOWED_HOSTS` | 是 | `ticket.example.com` | 逗号分隔，禁止 `*` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 是 | `https://ticket.example.com` | 必须带 `https://` 前缀 |
| `DJANGO_TIME_ZONE` | 否 | `Asia/Shanghai` | 默认 `Asia/Shanghai` |
| `DJANGO_SECURE_SSL_REDIRECT` | 否 | `True` | 生产 `True`；本地无 HTTPS 时置 `False` |
| `DJANGO_SECURE_HSTS_SECONDS` | 否 | `31536000` | 默认 1 年；首次上线建议先设 `300` 观察 |
| `DB_ENGINE` | 是 | `mysql` | `mysql`=MariaDB（生产）/ `sqlite`（本地） |
| `DB_NAME` | 是 | `ticket_system` | 库名，需与 `MARIADB_DATABASE` 一致 |
| `DB_USER` | 是 | `ticket` | 应用库账号 |
| `DB_PASSWORD` | 是 | `CHANGE_ME` | 应用库密码，compose 启动时强制校验 |
| `DB_HOST` / `DB_PORT` | 是 | `db` / `3306` | 容器内由 compose 强制指向 `db` |
| `DB_ROOT_PASSWORD` | 是 | `CHANGE_ME` | MariaDB root 密码，仅 compose 内使用 |
| `FERNET_KEY` | 是 | `$(openssl rand -base64 32 \| tr '+/' '-_')` | 邮箱凭据加密密钥，**丢失不可恢复** |
| `REDIS_URL` | 是 | `redis://redis:6379/0` | RQ broker；容器内由 compose 强制指向 `redis` |
| `IMAP_POLL_INTERVAL_SECONDS` | 否 | `60` | IMAP 轮询间隔 |
| `HOST_MEDIA_DIR` / `HOST_STATIC_DIR` | 否 | `/srv/ticket-system/media` | compose 绑定挂载的宿主路径 |
| `WEB_PORT` | 否 | `8000` | 宿主监听端口（仅 `127.0.0.1`） |
| `APP_ROLE` | compose 注入 | `web` | `web` / `worker` / `scheduler`，决定是否执行迁移与静态收集 |
| `BACKUP_DIR` / `BACKUP_RETENTION_DAYS` | 否 | `/srv/ticket-system/backups` / `14` | 备份目录与保留天数 |

---

## 9. 安全须知

对应开发文档 §10.3 的落地检查项：

- `.env` 已在 `.gitignore`（`.env`、`.env.*`）与 `.dockerignore` 中排除，**任何情况下不要提交或打进镜像**。
- `DJANGO_SECRET_KEY` / `FERNET_KEY` / `DB_PASSWORD` 只从环境变量注入；容器入口会拒绝以 `.env.example` 示例值启动（`SKIP_ENV_CHECK=1` 仅用于本地调试）。
- `DEBUG=False` 时自动开启：HSTS、`SESSION_COOKIE_SECURE`、`CSRF_COOKIE_SECURE`、`SECURE_CONTENT_TYPE_NOSNIFF`、`X_FRAME_OPTIONS=DENY`、`SECURE_REFERRER_POLICY=same-origin`。
- 邮箱授权码以 Fernet 密文存于 `mailboxes.secret_encrypted`，日志中只出现掩码（`apps/core/crypto.py::mask_secret`）。
- 附件路径经过 `safe_component()` 归一化，杜绝目录穿越；危险扩展名仅允许下载。
- **待整改项**：`/media/` 目前由 Nginx 直接对外服务，无身份校验，附件可被匿名下载，绕过工单可见性。整改方案（`X-Accel-Redirect` + 鉴权下载视图）与过渡期缓解措施见 [deploy/nginx/ticket.example.com.conf](deploy/nginx/ticket.example.com.conf) 中的注释。

---

## 10. 开发约定

- 模型先行：业务不变量写进 Model（外键、`UniqueConstraint`、`null=False`、`choices`），视图层不重复校验（开发文档 §10.1）。
- 改代码先跑测试再提交；每个功能一个 commit，message 格式 `[模块] 简述`（如 `[tickets] 实现工单归并逻辑`）。
- 不擅自新增第三方依赖、不手写迁移文件、不改 `settings.py` 的安全配置（开发文档 §10.4）。

---

## 11. MariaDB 关键注意点

这几条是本项目在 MariaDB 上最容易踩的坑，部署与排障前务必了解（详见 [docs/部署手册.md](docs/部署手册.md) 附录 B）：

1. **字符集必须 utf8mb4**：库、表、连接三处都要 utf8mb4，否则中文与 emoji 会截断或乱码。本项目的 `docker-compose.yml` 已用 `--character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci` 固定，`settings.py` 的连接参数固定 `charset=utf8mb4`。
2. **带 `condition` 的 `UniqueConstraint` 会被静默跳过**：Django 5.2 在 MySQL/MariaDB 上 `supports_partial_indexes=False`，`django/db/backends/base/schema.py::_unique_supported()` 返回 `False`，建表时不会生成该唯一索引，**也不会报错**。因此"全局唯一兜底邮箱"（`Mailbox.Meta.constraints` 里的 `unique_fallback_mailbox`）在 MariaDB 上没有任何数据库层保护，实际由应用层 `save()` / `clean()`（`_assert_fallback_unique`）保证。巡检时要主动核对 `SELECT count(*) FROM mailboxes WHERE is_fallback = 1` 是否 ≤ 1。
3. **500 字符索引字段的开销**：`tickets.normalized_subject`、`messages.message_id` 都是 `max_length=500` 且建索引。utf8mb4 下一个字符最多 4 字节，即索引键最长 2000 字节；MariaDB 11 默认 `DYNAMIC` 行格式 + `innodb_large_prefix` 上限 3072 字节，所以能建成功，但**单个索引条目可达约 2KB，写入放大与索引体积都很可观**。若换成 `COMPACT` 行格式或关闭大前缀，会直接报 `ERROR 1071 Specified key was too long`。需要索引的字段建议控制在 191 字符以内或使用前缀索引。
4. **全文检索不可移植**：Django 的全文检索深度绑定 PostgreSQL，MariaDB 上只能用 `LIKE` 或原生 `MATCH...AGAINST`，MVP 阶段用 `LIKE`。
5. **严格模式**：`settings.py` 通过连接的 `init_command` 设置 `sql_mode='STRICT_TRANS_TABLES'`，compose 的 db 服务也把服务器默认 sql_mode 设为严格模式，避免静默截断。

---

## 12. FAQ

**Q1. `docker compose up` 报 `DB_PASSWORD 未设置：请先按 .env.example 配置 .env`？**
这是刻意设计的保护：compose 用 `${DB_PASSWORD:?}` / `${DB_ROOT_PASSWORD:?}` 强校验，缺配置时直接报错退出，而不是拿空密码把数据库跑起来。先 `cp .env.example .env` 并填入真实值再启动。

**Q2. 本地浏览器访问 127.0.0.1:8000 被无限 301 到 https？**
`.env.example` 里 `DJANGO_SECURE_SSL_REDIRECT=True`，而本地没有 HTTPS。本地开发把它改成 `False`（或 `DJANGO_DEBUG=True`），生产保持 `True`。

**Q3. `scheduler` 容器反复重启，日志显示 `Unknown command: 'runapscheduler'`？**
说明当前代码版本里 `apps/core/management/commands/runapscheduler.py` 还没合入。部署前先自检：

```bash
docker compose run --rm scheduler python manage.py help runapscheduler
```

命令存在再启动 scheduler；否则它会被 `restart: unless-stopped` 反复拉起。可用 `docker compose up -d web worker db redis` 先只起其余四个服务。

**Q4. `web` 启动失败：`无法创建媒体目录 /app/media/attachments`？**
宿主绑定挂载目录属主不对。容器以 uid/gid 10001 运行：

```bash
sudo chown -R 10001:10001 /srv/ticket-system/media /srv/ticket-system/staticfiles
```

**Q5. 页面没有样式、`/static/` 404？**
两条链路都要检查：`docker compose exec web python manage.py collectstatic --noinput` 是否成功；Nginx 的 `alias /srv/ticket-system/staticfiles/;` 是否指向真实挂载点，且 Nginx worker（通常 `www-data`）对 `/srv/ticket-system` 有 `o+rx` 权限。

**Q6. 附件下载 404？**
多为 `/media/` 的 `alias` 路径与 `HOST_MEDIA_DIR` 不一致，或附件目录属主被改成了 root。比对 `docker-compose.yml` 的 `HOST_MEDIA_DIR` 与 Nginx 配置里的 `alias`。

**Q7. `python manage.py check` 报错，能不能先部署？**
不能，先按错误类型处理：

| 报错 | 含义 | 处置 |
|---|---|---|
| `ModuleNotFoundError` / `ImportError: cannot import name 'xxx'` | 依赖未安装，或代码引用的模块/视图尚未合入当前版本 | 装齐 `requirements.txt`；确认代码版本完整（`git status`） |
| `django.core.exceptions.ImproperlyConfigured` | 环境变量缺失（如 `DJANGO_SECRET_KEY`） | 补齐 `.env` 后重试 |
| `makemigrations --check --dry-run` 报告有待生成迁移 | 模型改了但迁移没生成 | 由开发补齐迁移文件（本项目禁止手写迁移，见开发文档 §10.4） |
| `check --deploy` 的安全告警 | 生产配置不达标（如 `DEBUG=True`、`SECRET_KEY` 过弱） | 按提示修 `.env`，不要改 `settings.py` 的安全开关 |

`check`、`migrate`、`collectstatic` 都会先跑系统检查，所以 `check` 不过时后续命令必然失败。

**Q8. `pytest` 有用例失败，能部署吗？**
不能。CI 有两道关卡：`unit`（SQLite 单测 + `check` + `check --deploy` + `makemigrations --check`）与 `mariadb`（真实 MariaDB 上 `migrate` + 全量测试）。本地至少要让 `python -m pytest` 全绿；涉及 MariaDB 行为（字符集、索引长度、部分索引约束被跳过）的改动还要跑一次 MariaDB 集成测试（命令见 §6）。

**Q9. 为什么数据库里没有"全局唯一兜底邮箱"的唯一索引？**
见 §11 第 2 条：MariaDB 不支持部分索引，Django 静默跳过带 `condition` 的 `UniqueConstraint`。该不变量由应用层保证，巡检时用 SQL 核对。

**Q10. 恢复备份后出现了重复工单？**
备份是时间点快照，`mailboxes.last_uid` 会一并回退；恢复后若直接放开调度器，IMAP 会把备份点之后的邮件重新拉一遍。同一 `Message-ID` 有幂等保护（`already_seen`），所以**已在库中的邮件不会重复入库**，但备份点之后的新邮件会被重新建单、重新自动回复。恢复后必须先冻结同步并核对 `last_uid`/`uidvalidity`，步骤见 [docs/部署手册.md](docs/部署手册.md) §7。

**Q11. Redis 挂了会怎样？**
`apps/mailboxes/tasks.py::dispatch` 会先探测 Redis，不可用则**同步内联执行**并打警告日志（`RQ 不可用（…），改为同步执行 …`）。结果是队列不再积压，但对应请求变慢、发信在 Web 进程里完成。Redis 恢复后自动回到入队模式。

**Q12. 换了 `FERNET_KEY` 会怎样？**
所有 `mailboxes.secret_encrypted` 都解不开，报 `CredentialError: 凭据解密失败：FERNET_KEY 可能已更换或数据损坏`，需要逐个人工重录邮箱授权码。`FERNET_KEY` 必须长期稳定保存并单独备份。

**Q13. 首次上线要不要开 HSTS？**
`settings.py` 在 `DEBUG=False` 时默认下发 `SECURE_HSTS_SECONDS=31536000`（一年）并包含子域。一旦证书或 HTTPS 配置出问题，访客浏览器在有效期内会拒绝降级访问。建议首次上线先设 `DJANGO_SECURE_HSTS_SECONDS=300`，确认稳定后再调到 `31536000`，`DJANGO_SECURE_HSTS_PRELOAD` 保持 `False`（除非你确实要提交预加载列表）。

**Q14. 附件上传 413？**
Nginx `client_max_body_size 30M` 与数据库设置项 `max_attachment_size_mb`（默认 25）是两层限制。调大应用上限时，Nginx 侧要同步调大。
