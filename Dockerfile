# syntax=docker/dockerfile:1.7
# ===========================================================================
# 新技元邮箱及工单管理系统 · 生产镜像（开发文档 §8 技术栈 / §9 部署架构）
#
#   构建：docker compose build     （或 docker build -t oridesk/app:latest .）
#   运行：由 docker-compose.yml 编排。容器内 Gunicorn 监听 0.0.0.0:8000，
#         宿主机 Nginx 反向代理 127.0.0.1:8000 → 容器 8000。
#
# 设计要点：
#   1) 多阶段构建：builder 只负责把依赖装进独立 venv，runtime 不带编译工具链；
#   2) 先 COPY requirements.txt 再 COPY 代码，改代码不会触发依赖重装（缓存友好）；
#   3) 非 root 运行（uid/gid 10001），需配合宿主目录 chown 10001:10001；
#   4) .env 绝不进镜像（见 .dockerignore），密钥一律由 compose 的 env_file 注入；
#   5) HEALTHCHECK 指向 /healthz（apps/core/views.py，200=正常 / 503=数据库不可达）。
# ===========================================================================
ARG PYTHON_VERSION=3.12

# ============================ 阶段 1：依赖构建 ==============================
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# 只先 COPY 依赖清单：requirements.txt 未变时该层直接命中缓存。
# 注意：requirements.txt / requirements-dev.txt 由业务侧维护，本镜像只装运行时依赖。
COPY requirements.txt /tmp/requirements.txt

# requirements.txt 的依赖在 cp312 上都有 manylinux wheel
# （PyMySQL 为纯 Python；cryptography / argon2-cffi / gunicorn 均有预编译包），
# 因此无需安装 gcc 等构建工具。日后若引入需要编译的包，再在这一层补装。
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r /tmp/requirements.txt

# ============================ 阶段 2：运行时 ================================
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings \
    APP_HOME=/app \
    APP_ROLE=web

# 运行时系统包保持最小：不装 gcc / curl / mariadb-client。
#   - 健康检查用 Python 标准库 urllib；
#   - 数据库连通性检查用 PyMySQL（见 docker/entrypoint.sh）；
#   - 需要人工查库时用 `docker compose exec db mariadb ...`（官方镜像自带客户端）。
COPY --from=builder /opt/venv /opt/venv

# 固定 uid/gid 10001：与 docker-compose.yml、宿主机 /srv/ticket-system 的
# chown 10001:10001 保持一致，避免绑定挂载卷的权限漂移。
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR ${APP_HOME}

# 应用代码。.env / db.sqlite3 / media / staticfiles 已被 .dockerignore 排除。
COPY --chown=10001:10001 . ${APP_HOME}

# STATIC_ROOT / MEDIA_ROOT 必须与 config/settings.py 一致（staticfiles/、media/）。
# 这两个目录在 compose 中是宿主绑定挂载，这里的 mkdir 保证脱离 compose 单独
# `docker run` 时也能直接启动。
RUN chmod +x ${APP_HOME}/docker/entrypoint.sh \
 && mkdir -p ${APP_HOME}/staticfiles ${APP_HOME}/media/attachments \
 && chown -R 10001:10001 ${APP_HOME}/staticfiles ${APP_HOME}/media

USER 10001:10001

EXPOSE 8000

# 容器健康检查：GET /healthz。200 视为健康；503（数据库不可达）或连不上进程时
# urlopen 会抛错/返回非 200，命令以非 0 退出，容器被标记 unhealthy。
# 注意：worker / scheduler 不监听 8000，compose 中已为它们覆盖此检查。
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

# 入口：等待数据库/Redis → （web）migrate + collectstatic → exec 启动命令
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-b", "0.0.0.0:8000"]
