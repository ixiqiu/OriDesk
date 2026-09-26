"""apps.notifications：站内事件的移动端推送。

对应 `docs/移动端API约定.md`（已冻结 v1.0）：
- 阶段 1：受众解析、@提及、聚合、ntfy 发布、三处钩子
  （本模块的 models / audience / mentions / ntfy / services / tasks）
- 阶段 2：端点 E1–E6（本模块的 views / urls）

设计约束（契约 §1.4）：**零新增 Python 依赖**。ntfy 发布走 stdlib urllib，
topic 生成走 stdlib secrets，token 加密复用 apps.core.crypto。
"""
