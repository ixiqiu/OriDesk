"""Django 配置。

约定（开发文档 §8.2 / §10.3）：
- 安全相关配置一律来自环境变量，代码中不硬编码 SECRET_KEY / FERNET_KEY。
- DEBUG 默认 False，ALLOWED_HOSTS 默认 localhost，绝不允许 "*"。
- 数据库默认 SQLite（本地开发与单元测试），DB_ENGINE=mysql 时使用 MariaDB。
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 加载器，避免引入额外依赖。已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str | None = None) -> str | None:
    value = os.environ.get(key, default)
    return value


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 基础
DEBUG = env_bool("DJANGO_DEBUG", False)
UNDER_TEST = "pytest" in sys.modules or env_bool("DJANGO_TESTING", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or UNDER_TEST:
        # 仅本地调试/测试使用；生产必须由环境变量提供。
        SECRET_KEY = "dev-only-insecure-secret-key-do-not-use-in-production"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY 未设置：请通过环境变量注入，不要写入代码库。"
        )

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
if UNDER_TEST and "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "apps.core",
    "apps.accounts",
    "apps.mailboxes",
    "apps.tickets",
    "apps.routing",
    "apps.autoresponder",
    "apps.audit",
]

try:  # RQ 队列（文档 §8：RQ + Redis）。缺少依赖时不阻断 Web 启动。
    import django_rq  # noqa: F401

    INSTALLED_APPS.append("django_rq")
    HAS_DJANGO_RQ = True
except ImportError:  # pragma: no cover
    HAS_DJANGO_RQ = False

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.audit.middleware.AuditContextMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------- 数据库
DB_ENGINE = (env("DB_ENGINE", "sqlite") or "sqlite").lower()

if DB_ENGINE in ("mysql", "mariadb"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DB_NAME", "ticket_system"),
            "USER": env("DB_USER", "ticket"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "db"),
            "PORT": env("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            "TEST": {
                "CHARSET": "utf8mb4",
                "COLLATION": "utf8mb4_unicode_ci",
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:" if UNDER_TEST else (BASE_DIR / "db.sqlite3"),
            "OPTIONS": {"transaction_mode": "IMMEDIATE"},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- 认证
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 优先（文档 §8）
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# ---------------------------------------------------------------- 国际化
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [BASE_DIR / "locale"]

# ---------------------------------------------------------------- 静态与媒体
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "media")))

# 静态资源指纹（文档 §10.3 之外的工程约定）：
# 生产用 ManifestStaticFilesStorage —— collectstatic 会为每个静态文件生成带内容哈希的
# 文件名（app.3f2a1b.css）并写 staticfiles.json，{% static %} 自动指向哈希版。
# 这样改了 CSS/JS 后浏览器一定会取到新文件，不需要用户手动强刷，也不需要改模板。
# 本地开发与单元测试保持普通存储：测试不跑 collectstatic，用 manifest 会因缺清单报错。
_STATIC_BACKEND = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if (DEBUG or UNDER_TEST)
    else "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": _STATIC_BACKEND},
}

# 附件上传上限（文档 §4.6 max_attachment_size_mb 的兜底默认值）
_MAX_ATTACHMENT_MB = env_int("MAX_ATTACHMENT_SIZE_MB", 25)
DATA_UPLOAD_MAX_MEMORY_SIZE = _MAX_ATTACHMENT_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000

# ---------------------------------------------------------------- 缓存 / 队列
REDIS_URL = env("REDIS_URL", "redis://127.0.0.1:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ticket-system",
    }
}

RQ_QUEUES = {
    "default": {"URL": REDIS_URL, "DEFAULT_TIMEOUT": 600},
    "mail": {"URL": REDIS_URL, "DEFAULT_TIMEOUT": 600},
}

# IMAP 轮询间隔（秒），运行时可被数据库 Setting.imap_poll_interval_seconds 覆盖
IMAP_POLL_INTERVAL_SECONDS = env_int("IMAP_POLL_INTERVAL_SECONDS", 60)

# ---------------------------------------------------------------- 日志
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apps": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "imapclient": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "apscheduler": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# ---------------------------------------------------------------- 安全（文档 §10.3）
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
if UNDER_TEST:
    # 单元测试通过 Django test client 走 http://testserver：
    # 若沿用生产开关，SECURE_SSL_REDIRECT 会把每个请求先 301 到 https，
    # 断言"未登录跳转 302 / 越权 403"的用例全部失配（CI 上真实踩到过）。
    # 同时关闭 Secure Cookie 与 HSTS，保证测试结果与本地是否有 .env 无关。
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
elif not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
    CSRF_COOKIE_HTTPONLY = False  # HTMX 需要读取 csrftoken
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"

# Fernet 密钥（邮箱凭据加密，文档 §9.3）
FERNET_KEY = env("FERNET_KEY")
if not FERNET_KEY and (DEBUG or UNDER_TEST):
    # 与 SECRET_KEY 同样处理：本地调试/单元测试用**确定性派生**的开发密钥，
    # 让"没写 .env 也能跑测试"；生产（DEBUG=False 且非测试）不会走到这里，
    # 而是由 `manage.py deploy_check` 判 FAIL、运行时报明确的 CredentialError。
    import base64 as _b64
    import hashlib as _hashlib

    FERNET_KEY = _b64.urlsafe_b64encode(_hashlib.sha256(b"dev-fernet").digest()).decode()

# 附件下发的 X-Accel-Redirect 前缀（生产推荐，见 docs/安全清单核查.md）：
# 配置后 /media/attachments/ 可在 Nginx 侧设为 internal，只有通过 Django 鉴权的
# 请求才会被放行，直链访问一律 404。留空则由 Django 直接流式返回。
ATTACHMENT_X_ACCEL_PREFIX = env("ATTACHMENT_X_ACCEL_PREFIX", "")
