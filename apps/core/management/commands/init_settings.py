"""初始化系统配置与默认模板（开发文档 §4.6 / §2.4）。

用法：
    python manage.py init_settings           # 写入缺失的默认设置项
    python manage.py init_settings --force   # 覆盖为默认值
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.audit.models import Setting
from apps.autoresponder.services import DEFAULT_TEMPLATE
from apps.routing.models import Template


class Command(BaseCommand):
    help = "初始化系统设置项（§4.6）与全局自动回复模板（§2.4）"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="覆盖已有配置值")

    @transaction.atomic
    def handle(self, *args, **options):
        created, kept = 0, 0
        for key, value in Setting.DEFAULTS.items():
            exists = Setting.objects.filter(key=key).exists()
            if exists and not options["force"]:
                kept += 1
                continue
            Setting.objects.update_or_create(key=key, defaults={"value": value})
            created += 1
            self.stdout.write(f"  设置项 {key} = {value!r}")

        template = Template.objects.filter(scope="global").first()
        if template is None:
            Template.objects.create(scope="global", body=DEFAULT_TEMPLATE)
            self.stdout.write(self.style.SUCCESS("已创建全局自动回复模板。"))
        elif options["force"]:
            template.body = DEFAULT_TEMPLATE
            template.save(update_fields=["body", "updated_at"])
            self.stdout.write(self.style.SUCCESS("已重置全局自动回复模板。"))
        else:
            self.stdout.write("全局自动回复模板已存在，保持不变。")

        self.stdout.write(
            self.style.SUCCESS(f"完成：写入 {created} 项，跳过 {kept} 项已有配置。")
        )
