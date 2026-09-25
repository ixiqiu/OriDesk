#!/usr/bin/env python
"""Django 管理入口。"""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "无法导入 Django，请确认已激活虚拟环境并安装 requirements.txt。"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
