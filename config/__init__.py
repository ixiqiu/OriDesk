"""项目配置包。

MariaDB 驱动：优先使用 mysqlclient；不可用时回退 PyMySQL（开发文档 §8.1）。
PyMySQL 需要向 Django 谎报 mysqlclient 的版本号，否则 Django 会拒绝启动。
"""

try:  # pragma: no cover - 取决于部署环境安装了哪个驱动
    import MySQLdb  # noqa: F401
except ImportError:  # pragma: no cover
    try:
        import pymysql

        pymysql.version_info = (1, 4, 6, "final", 0)
        pymysql.install_as_MySQLdb()
    except ImportError:
        pass
