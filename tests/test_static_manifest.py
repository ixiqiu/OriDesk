"""静态资源指纹（ManifestStaticFilesStorage）回归测试。

背景：生产用 manifest 存储给静态文件加内容哈希（`app.3b0bf892619a.css`），这样改了
CSS/JS 浏览器一定会取到新文件，用户不需要手动强刷。

代价是它要求静态文件里**所有被引用到的资源**都真实存在：CSS 的 `url()` / `@import`、
JS 的 `sourceMappingURL` 少一个，`collectstatic` 就会直接失败 —— 而收集静态文件正是
容器启动的第一步，于是表现成"部署起不来"。

2026-09-26 真实踩到过：`static/vendor/quill.js` 尾部引用了仓库里并未发布的
`quill.js.map`。这里把"生产存储能跑通 collectstatic"变成 CI 可拦的门槛。
"""

from __future__ import annotations

import json

from django.core.management import call_command
from django.test import override_settings

MANIFEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

# 必须生成哈希版本的关键静态资源（改这些文件时应保持可指纹化）
KEY_ASSETS = ("css/app.css", "js/nav.js", "vendor/quill.js", "vendor/quill.snow.css")


def test_collectstatic_with_manifest_storage_succeeds(tmp_path):
    """生产静态存储下 collectstatic 必须成功，并为关键资源生成带哈希的文件名。"""
    with override_settings(STATIC_ROOT=str(tmp_path), STORAGES=MANIFEST_STORAGES):
        call_command("collectstatic", "--noinput", verbosity=0)

        manifest_file = tmp_path / "staticfiles.json"
        assert manifest_file.exists(), "collectstatic 未生成 staticfiles.json"

        paths = json.loads(manifest_file.read_text(encoding="utf-8"))["paths"]
        for name in KEY_ASSETS:
            assert name in paths, f"{name} 未进入 manifest"
            hashed = paths[name]
            assert hashed != name, f"{name} 没有被指纹化（仍是原名）"
            assert (tmp_path / hashed).exists(), f"{name} 的哈希文件不存在：{hashed}"
