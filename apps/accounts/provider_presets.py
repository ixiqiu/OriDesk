"""邮箱服务商预设（图形化配置的"下拉即填"）。

系统本身只依赖标准 IMAP/SMTP（`apps/mailboxes/imap_client.py` + `apps/mailboxes/services.py`），
不绑定任何服务商。本文件提供常见服务商的连接参数与注意事项，目的是让管理员在界面上
**选一个服务商就自动填好主机/端口/加密方式**，也可以完全手填（"自定义/自建"）。

字段含义：
- `imap_ssl` / `smtp_ssl` 为 True 表示**隐式 TLS**（连接即加密，常用 993/465）；
  为 False 表示**明文连接后升级 STARTTLS**（常用 143/587），服务器不支持 STARTTLS 时会报错而不会明文登录。
- `auth_hint` 是给管理员的填写提示：很多服务商要求填「授权码」而不是登录密码。
"""

from __future__ import annotations

MAILBOX_PROVIDER_PRESETS: list[dict] = [
    {
        "key": "feishu",
        "label": "飞书企业邮箱",
        "imap_host": "imap.feishu.cn",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.feishu.cn",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "在飞书管理后台为账号开启 IMAP/SMTP 并生成专用密码（授权码），用户名填完整邮箱地址。",
    },
    {
        "key": "tencent_exmail",
        "label": "腾讯企业邮箱（exmail）",
        "imap_host": "imap.exmail.qq.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.exmail.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "在「邮箱设置 → 客户端专用密码」生成授权码作为密码；需管理员开启 IMAP/SMTP 服务。",
    },
    {
        "key": "qq_mail",
        "label": "QQ 邮箱（个人版，仅测试用）",
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "设置 → 账号 → 开启 IMAP/SMTP 服务，生成 16 位授权码作为密码。个人邮箱有发信频率限制，不建议生产使用。",
    },
    {
        "key": "aliyun",
        "label": "阿里云企业邮箱",
        "imap_host": "imap.qiye.aliyun.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.qiye.aliyun.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "使用完整邮箱地址与登录密码；如账号启用了安全策略，请在阿里云后台确认 IMAP/SMTP 已开放。",
    },
    {
        "key": "netease_163",
        "label": "网易 163/126 邮箱",
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "在「设置 → POP3/SMTP/IMAP」开启服务并获取授权码；网易（Coremail）要求客户端发送 IMAP ID，系统已自动处理。",
    },
    {
        "key": "gmail",
        "label": "Google Workspace / Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "需开启两步验证并生成「应用专用密码」作为密码（Google 已不接受账号密码直连）；如组织强制 OAuth2，请见 docs/邮箱接入指南.md 的限制说明。",
    },
    {
        "key": "outlook365",
        "label": "Microsoft 365 / Outlook",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_ssl": False,  # 587 走 STARTTLS
        "auth_hint": "微软默认已停用 IMAP/SMTP 的基础认证（Basic Auth），必须由管理员为账号启用 SMTP AUTH，或改用支持 OAuth2 的方案（见 docs/邮箱接入指南.md）。",
    },
    {
        "key": "zoho",
        "label": "Zoho Mail",
        "imap_host": "imap.zoho.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.zoho.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "使用完整邮箱与密码/应用专用密码；如开启了两步验证需生成应用密码。",
    },
    {
        "key": "self_hosted",
        "label": "自建邮件服务器（Postfix/Dovecot 等）",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "按你自己的证书与端口填写：993/465 用隐式 TLS；143/587 请把 SSL 开关关掉（系统会强制 STARTTLS）。",
    },
    {
        "key": "custom",
        "label": "自定义 / 其他服务商",
        "imap_host": "",
        "imap_port": 993,
        "imap_ssl": True,
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_ssl": True,
        "auth_hint": "任何支持标准 IMAP/SMTP 的服务商都可以接入：填好主机、端口与加密开关即可。",
    },
]

PRESETS_BY_KEY: dict[str, dict] = {item["key"]: item for item in MAILBOX_PROVIDER_PRESETS}

# 供表单 ChoiceField 使用的选项（首项为"不套用预设"）
PROVIDER_CHOICES = [("", "— 不套用预设（手动填写）—")] + [
    (item["key"], item["label"]) for item in MAILBOX_PROVIDER_PRESETS
]
