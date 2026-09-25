"""apps.mailboxes：邮箱接入（IMAP / SMTP / 解析 / 防循环 / 净化）。"""

from django.apps import AppConfig


class MailboxesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mailboxes"
    label = "mailboxes"
    verbose_name = "邮箱接入"
