"""apps.routing：规则、路由引擎、粘性、兜底。"""

from django.apps import AppConfig


class RoutingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.routing"
    label = "routing"
    verbose_name = "路由"
