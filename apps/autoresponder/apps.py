"""apps.autoresponder：自动回复与模板。"""

from django.apps import AppConfig


class AutoresponderConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.autoresponder"
    label = "autoresponder"
    verbose_name = "自动回复"
