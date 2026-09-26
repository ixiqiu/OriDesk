"""上线前自检：把《部署手册》的检查清单变成一条命令（开发文档 §9 / §10.3）。

用法：
    python manage.py deploy_check             # 有 FAIL 时退出码 1
    python manage.py deploy_check --strict    # WARN 也视为失败（用于上线闸门 / CI）

设计原则与项目其它部分一致：**失败要响**。检测到的每一项都给出"现象 + 处理建议"，
而不是只抛一个 traceback；同时区分：
    OK   —— 已满足
    WARN —— 可以上线，但请知情（例如用 SQLite 跑生产、未配置兜底组）
    FAIL —— 不应上线（密钥缺失/非法、数据库不可达、有待应用的迁移、凭据不可解、路径不可写…）
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

_STYLE = {OK: "SUCCESS", WARN: "WARNING", FAIL: "ERROR"}


class Command(BaseCommand):
    help = "上线前自检：环境变量、密钥、数据库、迁移、邮箱凭据、路由兜底、存储与队列"

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="把 WARN 也当作失败（返回非零退出码），适合放在上线流程的最后一道闸门",
        )

    def handle(self, *args, **options):
        self.results: list[tuple[str, str, str]] = []
        self.db_connected = False   # 数据库能否连上
        self.schema_current = False  # 表结构是否已就绪（迁移全部应用）

        self._check_django_settings()
        self._check_test_mode()
        self._check_fernet()
        self._check_database()
        self._check_migrations()
        # 依赖表结构的检查：DB 不可用/迁移未应用时不能直接查库，
        # 否则首次部署（还没 migrate）会以 traceback 结束而不是给出可读结论。
        for check in (self._check_mailboxes, self._check_routing, self._check_autoresponder):
            self._run_db_check(check)
        self._check_storage()
        self._check_queue()

        return self._report(strict=options["strict"])

    def _run_db_check(self, check) -> None:
        """执行依赖数据库的检查，把数据库异常转成可读结论而不是崩溃。"""
        from django.db import DatabaseError

        if not self.schema_current:
            self._record(
                WARN,
                f"跳过{self._skip_target(check)}：数据库结构尚未就绪",
                "先执行 python manage.py migrate（容器内由 entrypoint 自动执行），再重跑 deploy_check",
            )
            return
        try:
            check()
        except DatabaseError as exc:
            self._record(
                FAIL,
                f"{self._skip_target(check)}时数据库报错：{exc}",
                "确认数据库可用、迁移已应用后重跑",
            )

    @staticmethod
    def _skip_target(check) -> str:
        return {
            "_check_mailboxes": "邮箱配置检查",
            "_check_routing": "路由与用户组检查",
            "_check_autoresponder": "自动回复模板检查",
        }.get(getattr(check, "__name__", ""), "数据库检查")

    # ------------------------------------------------------------------ 各项检查
    def _record(self, level: str, title: str, hint: str = "") -> None:
        self.results.append((level, title, hint))

    def _check_django_settings(self) -> None:
        if settings.DEBUG:
            self._record(FAIL, "DEBUG 仍为 True", "生产必须 DJANGO_DEBUG=False（.env）")
        else:
            self._record(OK, "DEBUG=False")

        hosts = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
        if not hosts:
            self._record(FAIL, "ALLOWED_HOSTS 为空", "设置 DJANGO_ALLOWED_HOSTS=你的域名")
        elif "*" in hosts:
            self._record(FAIL, "ALLOWED_HOSTS 含通配符 *", "改成具体域名，禁止 *（§10.3）")
        else:
            self._record(OK, f"ALLOWED_HOSTS={','.join(hosts)}")

        secret = getattr(settings, "SECRET_KEY", "") or ""
        if secret.startswith("django-insecure") or len(secret) < 50:
            self._record(
                FAIL,
                f"SECRET_KEY 过弱或为开发占位值（长度 {len(secret)}）",
                "用 openssl rand -base64 48 生成 ≥50 位随机串写入 .env",
            )
        else:
            self._record(OK, "SECRET_KEY 已由环境变量注入且长度合规")

        if getattr(settings, "DEBUG", False) is False and not settings.SECURE_SSL_REDIRECT:
            self._record(WARN, "SECURE_SSL_REDIRECT 已关闭", "确认由 Nginx 负责 HTTP→HTTPS 跳转")
        else:
            self._record(OK, "HTTPS 跳转已启用（或处于调试模式）")

    def _check_test_mode(self) -> None:
        """生产环境严禁处于"测试模式"。

        `config/settings.py` 在 `UNDER_TEST`（pytest 已加载，或显式设置 `DJANGO_TESTING=1`）下会
        主动关闭 `SECURE_SSL_REDIRECT` / Secure Cookie / HSTS——这是为了让单元测试能跑 http。
        如果有人把 `DJANGO_TESTING=1` 带进生产 `.env`，安全开关会被**静默**关掉，
        因此这里单独判 FAIL（DEBUG 检查发现不了它，因为 DEBUG 仍是 False）。
        """
        env_flag = (os.environ.get("DJANGO_TESTING", "") or "").strip().lower() in ("1", "true", "yes", "on")
        if getattr(settings, "UNDER_TEST", False) or env_flag:
            self._record(
                FAIL,
                "检测到测试模式（UNDER_TEST / DJANGO_TESTING）",
                "该模式会关闭 HTTPS 强制跳转、Secure Cookie 与 HSTS；请从生产 .env 中删除 DJANGO_TESTING",
            )
        else:
            self._record(OK, "未处于测试模式（生产安全开关正常生效）")

    def _check_fernet(self) -> None:
        key = getattr(settings, "FERNET_KEY", None)
        if not key:
            self._record(
                FAIL,
                "FERNET_KEY 未配置",
                "邮箱凭据无法解密：用 python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\" 生成并写入 .env（务必独立备份）",
            )
            return
        try:
            from cryptography.fernet import Fernet

            Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except Exception as exc:  # noqa: BLE001
            self._record(FAIL, f"FERNET_KEY 格式非法：{exc}", "应为 urlsafe base64 的 32 字节密钥")
        else:
            self._record(OK, "FERNET_KEY 格式合法")

    def _check_database(self) -> None:
        alias = "default"
        engine = settings.DATABASES[alias]["ENGINE"]
        if "sqlite" in engine:
            self._record(
                WARN,
                "数据库使用 SQLite",
                "生产文档要求 MariaDB 10.11+（utf8mb4）；SQLite 仅用于本地开发与单元测试",
            )
        try:
            connection = connections[alias]
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            self._record(FAIL, f"数据库不可达：{exc}", "检查 DB_HOST/DB_USER/DB_PASSWORD 与网络")
            return
        self.db_connected = True
        self._record(OK, f"数据库可连接（{engine.rsplit('.', 1)[-1]}）")

        if "mysql" in engine:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT @@character_set_database, @@collation_database")
                    charset, collation = cursor.fetchone()
            except Exception as exc:  # noqa: BLE001 - 权限不足等
                self._record(WARN, f"无法确认库字符集：{exc}", "手工执行 SHOW VARIABLES LIKE 'character_set%'")
            else:
                if str(charset).lower() == "utf8mb4":
                    self._record(OK, f"库字符集 {charset} / {collation}")
                else:
                    self._record(
                        FAIL,
                        f"库字符集是 {charset}（应为 utf8mb4）",
                        "中文与 emoji 会出问题：重建库为 utf8mb4/utf8mb4_unicode_ci（§8.1）",
                    )

    def _check_migrations(self) -> None:
        try:
            connection = connections["default"]
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        except Exception as exc:  # noqa: BLE001
            self._record(WARN, f"无法检查迁移状态：{exc}", "确认数据库可用后重跑")
            return
        if plan:
            # Django 的 migration_plan() 返回 [(Migration, backwards?), ...]；
            # 这里对"元素是 Migration"与"元素是 (Migration, bool)"两种形态都兼容，
            # 避免在"空库/首次部署"这个最需要该提示的场景里抛 TypeError。
            names = ", ".join(self._migration_label(item) for item in plan[:5])
            suffix = " 等" if len(plan) > 5 else ""
            self._record(
                FAIL,
                f"存在 {len(plan)} 个未应用的迁移：{names}{suffix}",
                "执行 python manage.py migrate（容器里由 entrypoint 自动执行）",
            )
        else:
            self.schema_current = True
            self._record(OK, "数据库迁移已全部应用")

    @staticmethod
    def _migration_label(item) -> str:
        """把 migration_plan() 的元素格式化成 `app.0001_xxx`。"""
        migration = item[0] if isinstance(item, (tuple, list)) else item
        return f"{migration.app_label}.{migration.name}"

    def _check_mailboxes(self) -> None:
        from apps.accounts.models import Mailbox

        total = Mailbox.objects.count()
        active = Mailbox.objects.filter(is_active=True)
        if total == 0:
            self._record(FAIL, "系统里还没有任何邮箱", "登录后到「管理 → 邮箱配置」新建邮箱")
            return
        self._record(OK, f"邮箱配置 {total} 个（启用 {active.count()} 个）")

        if not active.exists():
            self._record(FAIL, "没有启用的邮箱", "启用至少一个邮箱用于收信")
            return

        # 凭据可在本机解密（换过 FERNET_KEY 或手工改库会失败）
        broken = []
        for mailbox in active:
            try:
                mailbox.get_secret()
            except Exception as exc:  # noqa: BLE001
                broken.append(f"{mailbox.email}（{exc}）")
        if broken:
            self._record(
                FAIL,
                f"{len(broken)} 个邮箱的凭据无法解密：{'; '.join(broken[:3])}",
                "FERNET_KEY 是否被更换？到「邮箱配置」重新录入授权码",
            )
        else:
            self._record(OK, "所有启用邮箱的凭据均可解密")

        fallbacks = Mailbox.objects.filter(is_fallback=True)
        if fallbacks.count() > 1:
            self._record(
                FAIL,
                f"存在 {fallbacks.count()} 个全局兜底邮箱（应唯一）",
                "MariaDB 不支持带条件的唯一索引，需人工清理（保留一个后重新保存）",
            )
        elif fallbacks.count() == 0:
            self._record(WARN, "未标记全局兜底邮箱", "无邮箱组的对外回信将无法发送（§2.1）")
        else:
            self._record(OK, f"全局兜底邮箱：{fallbacks.first().email}")

        if not Mailbox.objects.filter(group__isnull=False).exists():
            self._record(
                WARN,
                "没有邮箱绑定到任何用户组",
                "组邮箱用于以组身份收发信；可在「用户组管理」里绑定",
            )

    def _check_routing(self) -> None:
        from apps.accounts.models import Group
        from apps.routing.services import admin_group, fallback_group

        groups = Group.objects.count()
        if groups == 0:
            self._record(FAIL, "还没有任何用户组", "先建用户组，否则来信无处路由（会抛 RoutingError）")
            return
        self._record(OK, f"用户组 {groups} 个")

        if not Group.objects.filter(is_admin_group=True).exists():
            self._record(FAIL, "没有管理员组", "管理员组是权限容器与兜底归宿（§2.7）")
        else:
            self._record(OK, f"管理员组：{admin_group().name}")

        if fallback_group():
            self._record(OK, f"兜底组：{fallback_group().name}")
        else:
            self._record(
                WARN,
                "未配置兜底组（fallback_group_id）",
                "未命中规则的来信将进入管理员组；建议在「系统设置」里指定兜底组",
            )

        # 无邮箱组必须能靠全局兜底邮箱发信（§2.1 / §11-9）
        from apps.accounts.models import Mailbox

        no_mailbox = Group.objects.filter(mailbox__isnull=True)
        has_fallback_mailbox = Mailbox.objects.filter(is_fallback=True, is_active=True).exists()
        if no_mailbox.exists() and not has_fallback_mailbox:
            names = ", ".join(no_mailbox.values_list("name", flat=True)[:3])
            self._record(
                FAIL,
                f"无邮箱组（{names}）无法发信：既未指定兜底邮箱（fallback_mailbox_id），"
                "也没有 is_fallback=True 的邮箱",
                "在「邮箱配置」里指定一个全局兜底邮箱",
            )

    def _check_storage(self) -> None:
        media_root = Path(settings.MEDIA_ROOT)
        static_root = Path(settings.STATIC_ROOT)
        for label, path in (("MEDIA_ROOT", media_root), ("STATIC_ROOT", static_root)):
            try:
                path.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path, prefix=".deploy-check-", delete=True):
                    pass
            except Exception as exc:  # noqa: BLE001
                self._record(FAIL, f"{label} 不可写：{path}（{exc}）", "检查属主（容器内 uid 10001）与挂载")
            else:
                self._record(OK, f"{label} 可写：{path}")

        prefix = getattr(settings, "ATTACHMENT_X_ACCEL_PREFIX", "") or ""
        if prefix:
            self._record(
                OK,
                f"附件走 X-Accel-Redirect（{prefix}）",
                "确认 Nginx 里 /media/attachments/ 为 internal 且 alias 指向媒体根目录",
            )
        else:
            self._record(
                WARN,
                "未配置 ATTACHMENT_X_ACCEL_PREFIX",
                "附件将由 Django 自己流式返回（可用，但大文件占 web 进程）；生产建议配合 Nginx internal",
            )

        if os.environ.get("DJANGO_DEBUG", "").lower() in ("1", "true", "yes", "on"):
            self._record(WARN, "环境变量里显式设置了 DJANGO_DEBUG=True", "生产请去掉这一行")

    def _check_queue(self) -> None:
        try:
            from django_rq import get_queue

            get_queue("mail").connection.ping()
        except Exception as exc:  # noqa: BLE001
            self._record(
                WARN,
                f"Redis/RQ 不可用：{exc}",
                "异步发信会退化为同步执行（功能可用）；如已部署 redis 请检查 REDIS_URL",
            )
        else:
            self._record(OK, "Redis/RQ 可用（异步任务与队列正常）")

    def _check_autoresponder(self) -> None:
        from apps.autoresponder.services import DEFAULT_TEMPLATE
        from apps.routing.models import Template

        template = Template.objects.filter(scope="global").first()
        if template is None:
            self._record(
                WARN,
                "没有全局自动回复模板",
                "将使用代码内置默认模板；可在「自动回复」里正式配置，或运行 manage.py init_settings",
            )
        elif not (template.body or "").strip():
            self._record(FAIL, "全局自动回复模板内容为空", "补上正文，否则自动回复会发出空邮件")
        else:
            self._record(OK, "全局自动回复模板已配置")

        if Template.objects.filter(scope="group").count() == 0:
            self._record(WARN, "没有组级自动回复模板", "组邮箱首次进线将使用全局模板（可接受）")
        else:
            self._record(OK, "组级自动回复模板已配置")
        _ = DEFAULT_TEMPLATE  # 内置兜底模板存在即视为可用

    # ------------------------------------------------------------------ 输出
    def _report(self, *, strict: bool) -> None:
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("上线前自检（deploy_check）"))
        for level, title, hint in self.results:
            marker = self.style.__getattribute__(_STYLE[level])(f"[{level:4}]")
            self.stdout.write(f"{marker} {title}")
            if hint and level in (WARN, FAIL):
                self.stdout.write(f"       ↳ {hint}")

        fails = sum(1 for level, _t, _h in self.results if level == FAIL)
        warns = sum(1 for level, _t, _h in self.results if level == WARN)
        self.stdout.write("")
        self.stdout.write(f"结果：{fails} 项 FAIL，{warns} 项 WARN，{len(self.results) - fails - warns} 项 OK")

        if fails or (strict and warns):
            # 把不通过的条目也放进异常消息：容器/CI 里 stderr 才是被看到的那一份日志
            problems = [
                f"{level}: {title}"
                for level, title, _hint in self.results
                if level == FAIL or (strict and level == WARN)
            ]
            raise CommandError(
                f"自检未通过（FAIL={fails}，WARN={warns}{'，strict 模式' if strict else ''}）：\n  - "
                + "\n  - ".join(problems)
                + "\n请按上面的 ↳ 提示处理后重试。"
            )
        self.stdout.write(self.style.SUCCESS("自检通过，可以上线。"))
