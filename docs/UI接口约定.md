# Web 界面接口约定（前后端契约）

> 前后端不分离：Django 模板 + HTMX + Quill（开发文档 §8）。
> 本文件冻结 URL 名称、模板块名、上下文变量与权限要求，多个模块并行开发时以此为准。

## 1. URL 名称（不可更改）

| 名称 | 路径 | 视图 | 权限 |
|---|---|---|---|
| `accounts:login` | `/accounts/login/` | `django.contrib.auth.views.LoginView` | 匿名 |
| `accounts:logout` | `/accounts/logout/` | `LogoutView`（必须 POST） | 登录 |
| `accounts:password_change` | `/accounts/password/` | `PasswordChangeView` | 登录 |
| `accounts:password_change_done` | `/accounts/password/done/` | `PasswordChangeDoneView` | 登录 |
| `accounts:profile` | `/accounts/profile/` | `views.profile` | 登录 |
| `accounts:user_list` / `user_create` / `user_edit` | `/accounts/users/…` | `views.user_*` | 超级管理员 |
| `accounts:group_list` / `group_create` / `group_edit` | `/accounts/groups/…` | `views.group_*` | 超级管理员 |
| `accounts:mailbox_list` / `mailbox_create` / `mailbox_edit` / `mailbox_verify` | `/accounts/mailboxes/…` | `views.mailbox_*` | 超级管理员 |
| `tickets:inbox` / `mine` / `unassigned` / `awaiting` | `/`、`/mine/`、`/unassigned/`、`/awaiting/` | `views.inbox` | 登录 |
| `tickets:detail` | `/tickets/<pk>/` | `views.detail` | 可见性校验 |
| `tickets:reply` / `note` / `claim` / `unclaim` / `reassign` / `set_status` | POST | `views.*` | 可见性校验 |
| `tickets:message_list` | `/tickets/<pk>/messages/` | `views.message_list` | 可见性校验 |
| `tickets:attachment_download` / `attachment_preview` | `/attachments/<pk>/…` | `views.attachment_*` | 可见性校验 |
| `routing:rule_list` / `rule_create` / `rule_edit` / `rule_delete` / `rule_toggle` | `/routing/…` | `views.*` | 管理员 |
| `routing:settings` | `/routing/settings/` | `views.settings_edit` | 管理员 |
| `routing:mailbox_overview` | `/routing/mailboxes/` | `views.mailbox_overview` | 管理员 |
| `autoresponder:template_list` / `template_edit` / `group_template_edit` / `group_template_delete` | `/autoresponder/…` | `views.*` | 管理员 |
| `audit:log_list` / `log_detail` | `/audit/…` | `views.*` | 管理员 |
| `admin:index` | `/admin/` | Django Admin | staff |
| `healthz` | `/healthz` | JSON 健康检查 | 匿名 |

权限装饰器统一取自 `apps.core.permissions`：`login_required`、`superadmin_required`、`routing_manager_required`、`get_visible_ticket`。

## 2. 模板约定

- 布局模板：`templates/base.html`，可用块：`title`、`page_title`、`page_subtitle`、`page_actions`、`content`、`extra_head`、`extra_js`。
- 登录页 `templates/accounts/login.html` 为**独立页面**（不 extends base），使用 `.auth-page` / `.auth-card` 样式。
- 其余页面一律 `{% extends "base.html" %}`。
- 公共片段：`partials/_messages.html`（base 已包含）、`partials/_pagination.html`（需自带 `page_obj`）。
- 模板标签库 `{% load ui %}` 提供：`status_badge`、`awaiting_badge`、`direction_label`、`sender_label`、`qs_replace`（保留查询串）、`nav_active`。
- 样式类来自 `static/css/app.css`（Tailwind 风格子集 + 组件类：`.card`、`.btn`、`.btn-primary`、`.badge-*`、`.table`、`.alert-*`、`.form-row`、`.empty`）。禁止引入外部 CDN。
- 前端脚本一律放 `static/js/` 并在 `extra_js` 中引用；HTMX 已在 base 中加载（`static/vendor/htmx.min.js`）。
- 静态资源带内容哈希（生产 `ManifestStaticFilesStorage`）：`{% static %}` 会自动输出
  `app.<hash>.css` 这类指纹文件名，改完前端不需要用户清缓存；因此**不要手写静态资源路径**。
  改完 `static/` 下的文件无需其它操作，`collectstatic`（容器启动时执行）会重建清单。
- 新增的公共类（v2 视觉重设计，2026-09，均在 `app.css` / 由 `base.html` 渲染）：

| 类别 | 类名 | 说明 |
|---|---|---|
| 外壳 | `.rail` `.rail-brand` `.rail-sec` `.rail-item` `.rail-foot` | 桌面左导航（分组 + 计数）；≤900px 由 `@media` 转为抽屉 |
| 外壳 | `.m-tabbar` `.m-tab` | 窄屏底部 Tab（一级导航），桌面 `display:none` |
| 外壳 | `.nav-toggle` `#nav-toggle` / `.nav-backdrop` `#nav-backdrop` | 窄屏汉堡按钮与遮罩，逻辑在 `static/js/nav.js` |
| 列表 | `.list-toolbar` `.list-search` `.chips` `.chip` `.chip-active` `.list-count` | 搜索常驻 + 常用筛选 chips |
| 列表 | `.rows` `.rows-head` `.row` `.row-subject` `.row-sub` `.stripe*` | 宽屏宽松行列表；≤900px 同一份 DOM 转卡片流 |
| 弹层 | `.sheet-dialog` `.sheet` `.sheet-grip` `.sheet-head` `.sheet-body` `.sheet-foot` | 基于原生 `<dialog>.showModal()`（遮罩/Esc/焦点由浏览器负责），逻辑在 `static/js/sheet.js` |
| 通用 | `.icon` `.icon-sm` `.iconbtn` `.avatar` `.count` `.kv` `.badge-*`（浅底+圆点） | 图标一律内联 SVG；状态徽标统一「浅底 + 深字 + 圆点」 |

- 弹层用法：`<button data-sheet-open="<dialog id>">` 打开，内部 `[data-sheet-close]` 关闭，
  弹层内容放在 `<form>` 内即可随表单一起提交（列表页筛选就是这么用的）。


## 3. 上下文变量

### `tickets:inbox`
`page_obj`、`tickets`、`scope`（`all|mine|unassigned|awaiting`）、`filters`（dict：`q/group/status/awaiting/assignee`）、`form`、`groups`。

### `tickets:detail`
`ticket`、`timeline`（Message 列表）、`reply_form`、`note_form`、`reassign_form`、`status_form`、`audit_logs`、`identity_email`、`editor_id`。

### 全局（context processor `apps.core.context_processors.navigation`）
`nav_groups`、`nav_awaiting_total`、`unassigned_count`、`can_manage`、`can_manage_users`、`site_name`、`app_version`、`my_group_ids`、`nav_my_memberships`。

## 4. HTMX 约定

- 需要局部刷新时请求头 `HX-Request: true`，视图返回局部模板（如 `tickets/_inbox_table.html`、`tickets/_timeline.html`）。
- 写操作后需要跳转时返回 `204 + HX-Redirect`；普通请求走 PRG（POST → redirect → GET）。
- CSRF 由 `base.html` 的 `hx-headers` 统一携带。

## 5. 安全约定（开发文档 §10.3）

- 所有视图必须有权限装饰器；工单相关视图必须先做可见性校验。
- 所有写操作 `@require_POST` + CSRF。
- 用户输入的富文本入库前必须 `apps.mailboxes.sanitizer.sanitize_html`；纯文本用 `apps.core.utils.html_to_text`。
- 附件下载/预览必须先校验所属工单可见；危险扩展名（`Attachment.is_dangerous`）禁止在线预览。
- 邮箱凭据只以 Fernet 密文存储，页面与日志中永不出现明文（表单用 `PasswordInput`，留空表示不修改）。
- 列表页统一分页（默认每页 25 条），避免全表渲染。
