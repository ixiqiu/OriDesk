"""全局测试夹具（对所有测试生效，含 apps/*/tests/）。

`apps.audit.models.Setting` 使用 LocMemCache 缓存取值，而 Django 测试之间只回滚数据库、
**不会**清理缓存。若不显式清理，前一个用例写入的配置值会泄漏到后续用例，
导致"取决于执行顺序"的假绿/假红。这里统一在每个用例前后清空缓存。
"""

from __future__ import annotations

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    cache.clear()
    yield
    cache.clear()
