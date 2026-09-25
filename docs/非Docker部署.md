# 非 Docker 部署（systemd + Gunicorn + 宿主机 MariaDB/Redis）

> 适用场景：宿主机不方便跑 Docker，或已有 MariaDB / Redis / Nginx 想直接复用。
> 开发文档 §9 推荐 Docker Compose；本文是**等价的原生部署路径**，功能与安全要求完全一致
> （区别只在于：没有 entrypoint 帮你自动 `migrate`/`collectstatic`，需要手工执行或写进部署脚本）。

---

## 1. 前置条件

| 项 | 要求 | 检查命令 |
|---|---|---|
| 系统 | Debian 12 / Ubuntu 22.04+（其他发行版同理） | `cat /etc/os-release` |
| Python | 3.12 | `python3.12 -V` |
| MariaDB | 10.11+，`utf8mb4` | `mariadb --version` |
| Redis | 6+（RQ 队列/缓存） | `redis-cli ping` |
| Nginx | 已有站点在跑（复用） | `nginx -v` |
| 运行账号 | 独立系统用户（**不要用 root 跑应用**） | — |

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin oridesk
sudo mkdir -p /srv/ticket-system/{app,media,staticfiles,backups}
sudo chown -R oridesk:oridesk /srv/ticket-system
```

## 2. 数据库与 Redis 准备

```bash
sudo mariadb -e "
CREATE DATABASE ticket_system CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'ticket'@'127.0.0.1' IDENTIFIED BY '把这里换成强随机密码';
GRANT ALL PRIVILEGES ON ticket_system.* TO 'ticket'@'127.0.0.1';
FLUSH PRIVILEGES;"
```

MariaDB 服务端字符集建议写进 `/etc/mysql/mariadb.conf.d/99-oridesk.cnf`：

```ini
[mysqld]
character-set-server = utf8mb4
collation-server     = utf8mb4_unicode_ci
sql_mode             = STRICT_TRANS_TABLES
```

Redis 保持默认监听 `127.0.0.1:6379` 即可。

## 3. 代码与依赖

```bash
sudo -u oridesk git clone <你的仓库地址> /srv/ticket-system/app
cd /srv/ticket-system/app
sudo -u oridesk python3.12 -m venv .venv
sudo -u oridesk .venv/bin/pip install --upgrade pip
sudo -u oridesk .venv/bin/pip install -r requirements.txt    # 生产只装运行时依赖（不含 pytest/ruff）

# 用发布标签锁版本，便于回滚（本仓库的里程碑：v1.2.0 / v1.3.0 / v1.3.1 / v1.4.0）
sudo -u oridesk git checkout v1.4.0
```

## 4. 生产 `.env`

```bash
cd /srv/ticket-system/app
sudo -u oridesk cp .env.example .env

# 生成密钥（只在本机可见，务必另存备份）
openssl rand -base64 48                                   # → DJANGO_SECRET_KEY
.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"   # → FERNET_KEY
```

`/srv/ticket-system/app/.env` 关键项（其余见 `docs/部署手册.md` §3）：

```env
DJANGO_SECRET_KEY=<上面生成的 48 位随机串>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=ticket.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://ticket.example.com
DB_ENGINE=mysql
DB_NAME=ticket_system
DB_USER=ticket
DB_PASSWORD=<强随机密码>
DB_HOST=127.0.0.1
DB_PORT=3306
FERNET_KEY=<上面生成的 Fernet 密钥>
REDIS_URL=redis://127.0.0.1:6379/0
MEDIA_ROOT=/srv/ticket-system/media
STATIC_ROOT=/srv/ticket-system/staticfiles
DJANGO_SECURE_SSL_REDIRECT=True
ATTACHMENT_X_ACCEL_PREFIX=/media/attachments/    # 配合 Nginx internal，附件不外泄
```

```bash
sudo chown oridesk:oridesk /srv/ticket-system/app/.env
sudo chmod 600 /srv/ticket-system/app/.env        # 凭据文件仅运行用户可读
```

## 5. 初始化（手工等价于容器 entrypoint）

```bash
cd /srv/ticket-system/app
sudo -u oridesk .venv/bin/python manage.py migrate --noinput
sudo -u oridesk .venv/bin/python manage.py collectstatic --noinput
sudo -u oridesk .venv/bin/python manage.py init_settings          # §4.6 默认配置 + 全局自动回复模板

# 上线闸门：0 项 FAIL 才继续（--strict 连 WARN 都不放过）
sudo -u oridesk .venv/bin/python manage.py deploy_check
```

## 6. 三个 systemd 服务

`/etc/systemd/system/oridesk-web.service`

```ini
[Unit]
Description=OriDesk Web (Gunicorn)
After=network.target mariadb.service redis-server.service
Wants=mariadb.service redis-server.service

[Service]
Type=notify
User=oridesk
Group=oridesk
WorkingDirectory=/srv/ticket-system/app
EnvironmentFile=/srv/ticket-system/app/.env
ExecStartPre=/srv/ticket-system/app/.venv/bin/python manage.py migrate --noinput
ExecStartPre=/srv/ticket-system/app/.venv/bin/python manage.py collectstatic --noinput
ExecStart=/srv/ticket-system/app/.venv/bin/gunicorn config.wsgi:application \
    --bind 127.0.0.1:8000 \
    --workers 3 --threads 4 --timeout 65 \
    --access-logfile - --error-logfile -
Restart=always
RestartSec=5
# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/srv/ticket-system/media /srv/ticket-system/staticfiles

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/oridesk-worker.service`（RQ：发信/附件等异步任务）

```ini
[Unit]
Description=OriDesk RQ worker
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=simple
User=oridesk
Group=oridesk
WorkingDirectory=/srv/ticket-system/app
EnvironmentFile=/srv/ticket-system/app/.env
ExecStart=/srv/ticket-system/app/.venv/bin/python manage.py rqworker default mail
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/oridesk-scheduler.service`（IMAP 轮询，**全局只跑一个实例**）

```ini
[Unit]
Description=OriDesk IMAP scheduler (APScheduler)
After=network.target mariadb.service
Wants=mariadb.service

[Service]
Type=simple
User=oridesk
Group=oridesk
WorkingDirectory=/srv/ticket-system/app
EnvironmentFile=/srv/ticket-system/app/.env
ExecStart=/srv/ticket-system/app/.venv/bin/python manage.py runapscheduler
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oridesk-web oridesk-worker oridesk-scheduler
sudo systemctl status oridesk-web --no-pager
curl -fsS http://127.0.0.1:8000/healthz    # {"status":"ok","database":true}
```

> `--workers` 按核数调整（经验值 `2×CPU+1`，但本系统以 IO 等待为主，3~5 个 worker + 多线程更划算）。
> `runapscheduler` 只能起一个：多个实例会重复拉同一批邮件（虽然 `Message-ID` 幂等会兜底，但没必要）。

## 7. 创建管理员与业务数据

```bash
cd /srv/ticket-system/app
sudo -u oridesk .venv/bin/python manage.py createsuperuser --username admin --email admin@example.com
# 随后在管理界面（或 shell）把该账号设为应用层超级管理员：
sudo -u oridesk .venv/bin/python manage.py shell -c "
from apps.accounts.models import User
User.objects.filter(username='admin').update(is_superadmin=True, is_staff=True, is_superuser=True)"
```

然后登录 `https://ticket.example.com/`：
1. 「管理 → 用户组管理」建组（其中一个是**管理员组** `is_admin_group=True`）；
2. 「管理 → 邮箱配置」新建邮箱（选服务商预设 → 填账号与授权码 → **连通性检查**）；
3. 「管理 → 系统设置」指定**兜底组**与**兜底邮箱**；
4. 「管理 → 路由规则」配规则；
5. 「管理 → 邮箱与同步」看同步状态，或 `manage.py runapscheduler --once` 立刻拉一轮。

## 8. Nginx（复用仓库里的站点配置）

直接用 `deploy/nginx/ticket.example.com.conf`，只需确认两点：

- `upstream oridesk_app { server 127.0.0.1:8000; }`（原生部署就是本机 8000，无需改）；
- `location ^~ /media/attachments/ { internal; alias /srv/ticket-system/media/; }` 与 `.env` 的
  `ATTACHMENT_X_ACCEL_PREFIX=/media/attachments/` **成对**——附件只经 Django 鉴权后由 Nginx 内部转发。

```bash
sudo cp deploy/nginx/ticket.example.com.conf /etc/nginx/sites-available/oridesk.conf
sudo ln -s /etc/nginx/sites-available/oridesk.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d ticket.example.com        # 首次签发（详见 docs/部署手册.md §5）
```

## 9. 日志与备份

- **日志**：systemd 单元把 access/error 输出到 stdout，用 `journalctl` 查看：
  ```bash
  sudo journalctl -u oridesk-web -f
  sudo journalctl -u oridesk-scheduler --since "1 hour ago"
  ```
  需要文件日志可在 `ExecStart` 里改成 gunicorn 的 `--access-logfile /var/log/oridesk/access.log`，
  并配 `logrotate`。
- **备份**：`scripts/backup.sh` 支持"非容器化 + 宿主客户端"路径：
  ```bash
  sudo -u oridesk BACKUP_DB_HOST=127.0.0.1 BACKUP_DB_PORT=3306 \
      BACKUP_DIR=/srv/ticket-system/backups BACKUP_RETENTION_DAYS=14 \
      bash scripts/backup.sh
  # 每日 03:30 定时
  echo '30 3 * * * oridesk cd /srv/ticket-system/app && BACKUP_DB_HOST=127.0.0.1 BACKUP_DIR=/srv/ticket-system/backups bash scripts/backup.sh >> /var/log/oridesk-backup.log 2>&1' \
    | sudo tee /etc/cron.d/oridesk-backup
  ```
  恢复用 `scripts/restore.sh`（会先做现状快照），恢复后**必须核对 IMAP 位点**：
  见 `docs/部署手册.md` §7.4。

## 10. 升级与回滚

```bash
cd /srv/ticket-system/app
sudo -u oridesk .venv/bin/python manage.py deploy_check      # 升级前先确认现状健康
sudo -u oridesk git fetch --tags && sudo -u oridesk git checkout v1.4.0
sudo -u oridesk .venv/bin/pip install -r requirements.txt
sudo -u oridesk .venv/bin/python manage.py migrate --noinput
sudo -u oridesk .venv/bin/python manage.py collectstatic --noinput
sudo -u oridesk .venv/bin/python manage.py deploy_check
sudo systemctl restart oridesk-web oridesk-worker oridesk-scheduler
```

回滚 = 切回上一个标签 + `systemctl restart`；**迁移不可逆**，涉及数据变更的版本要按
`docs/部署手册.md` §6.3 用"升级前备份"恢复。

## 11. 与 Docker 路径的差异对照

| 项 | Docker Compose | 原生 systemd |
|---|---|---|
| `migrate` / `collectstatic` | entrypoint 自动（仅 web 角色） | 手工执行，或写进 `ExecStartPre`（本文已写） |
| 进程隔离 | 容器 + `init: true` | systemd + `NoNewPrivileges` / `ProtectSystem` |
| 文件属主 | 容器内 uid 10001，宿主需 chown | 运行用户 `oridesk`，宿主需 chown |
| 密钥注入 | `env_file: .env` | `EnvironmentFile=.env` |
| 健康检查 | compose healthcheck 打 `/healthz` | `curl /healthz` 或 `systemd` 无内置（可加 timer） |
| 上线自检 | `manage.py deploy_check`（可加进部署脚本） | 同上 |
| 备份 | `scripts/backup.sh`（容器内 mariadb-dump） | `scripts/backup.sh`（宿主客户端，见 §9） |

两条路径的应用代码、`.env` 变量、Nginx 配置、附件与静态目录约定**完全一致**，可以互相迁移。
