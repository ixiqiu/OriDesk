"""HTML / 纯文本净化（开发文档 §6.2）。

对外接口（已冻结，其他模块按此调用）：

- :func:`sanitize_html` —— 白名单净化 HTML，并做远程图片占位。
- :func:`sanitize_text` —— 把任意文本洗成安全的纯文本。

§6.2 只给出了 ``bleach.clean`` 的基础白名单；本模块在其之上补齐需求中的
三件事：

1. 远程图片占位：``<img src="http(s)://...">`` 改为
   ``src=/static/img/remote-image-placeholder.svg``，原地址存 ``data-src``，
   并加 ``class="remote-image" data-remote="1"``，用户点击后再加载。
2. ``cid:`` 内联图片不加占位（保留原始 ``cid:`` 地址）。
3. 内联 ``style`` 安全化：``expression()`` / ``javascript:`` / ``vbscript:``
   / ``url(...)`` 非 http(s) 等一律剔除。

注意：``bleach>=6`` 在没有 ``css_sanitizer`` 时会把 ``style`` 整体清空，
而本环境未安装 ``tinycss2``（``bleach.css_sanitizer`` 依赖它），因此这里用
纯标准库实现了一个满足 ``css_sanitizer`` 鸭子类型（``sanitize_css``）的对象。
"""

from __future__ import annotations

import html as html_module
import re

import bleach

# ------------------------------------------------------------------ 白名单
# 开发文档 §6.2 ALLOWED_TAGS / ALLOWED_ATTRS
ALLOWED_TAGS = [
    "p", "br", "b", "strong", "i", "em", "u",
    "a", "img", "ul", "ol", "li", "blockquote",
    "table", "thead", "tbody", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "span", "div",
]

# 开发文档 §6.2 的白名单，外加 img 的 data-cid（用于在净化过程中保住 cid: 内联图片）。
ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "img": ["src", "alt", "width", "height", "data-cid"],
    "span": ["style"],
    "div": ["style"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}

# 开发文档 §6.2：仅允许 http / https / mailto（cid: 由 data-cid 中转保护）。
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]

#: 远程图片占位图（开发文档 §6.2「远程图片默认替换为占位」）。
REMOTE_IMAGE_PLACEHOLDER = "/static/img/remote-image-placeholder.svg"
REMOTE_IMAGE_CLASS = "remote-image"

#: 需要连同内容一起整体丢弃的标签（script/style 的正文不是可见文本，保留反而危险）。
_DROP_ELEMENT_RE = re.compile(
    r"(?is)<(script|style|iframe|object|embed|applet|noscript|template|svg|math)\b.*?</\1\s*>"
)
_DROP_UNCLOSED_RE = re.compile(
    r"(?is)<(script|style|iframe|object|embed|applet|noscript|template|svg|math)\b.*\Z"
)
_DROP_STRAY_TAG_RE = re.compile(
    r"(?is)</?(script|style|iframe|object|embed|applet|noscript|template|svg|math)\b[^>]*>"
)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_SRC_ATTR_RE = re.compile(
    r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""", re.I
)
_ATTR_RE = re.compile(r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)\"""")
_HTTP_SRC_RE = re.compile(r"^https?://", re.I)
_CID_SRC_RE = re.compile(r"^cid:[^\s\"'<>]+$", re.I)
_EMPTY_STYLE_RE = re.compile(r"""\s+style\s*=\s*(""|'')""", re.I)

# ------------------------------------------------------------- 内联样式白名单
_ALLOWED_CSS_PROPERTIES = {
    "color", "background", "background-color", "background-image",
    "font", "font-family", "font-size", "font-weight", "font-style",
    "text-align", "text-decoration", "text-indent", "text-transform",
    "text-overflow", "line-height", "letter-spacing", "word-break",
    "word-wrap", "overflow-wrap", "white-space", "vertical-align", "direction",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-color", "border-style", "border-width", "border-collapse",
    "border-spacing", "border-radius",
    "width", "height", "max-width", "min-width", "max-height", "min-height",
    "display", "float", "clear", "list-style", "list-style-type",
    "table-layout", "caption-side",
}

# expression() 是 IE 时代的 JS 执行入口；javascript:/vbscript:/behavior/
# -moz-binding 同理。出现即整条声明丢弃。
_DANGEROUS_CSS_RE = re.compile(
    r"(expression\s*\(|javascript\s*:|vbscript\s*:|behavior\s*:|-moz-binding|@import|<|>)",
    re.I,
)
_CSS_URL_RE = re.compile(r"""url\s*\(\s*['"]?([^'")]*)""", re.I)


class _InlineStyleSanitizer:
    """极简 CSS 声明过滤器，满足 ``bleach`` 的 ``css_sanitizer`` 接口。"""

    def sanitize_css(self, style: str) -> str:
        if not style:
            return ""
        kept: list[str] = []
        for declaration in style.split(";"):
            declaration = declaration.strip()
            if not declaration or ":" not in declaration:
                continue
            prop, _, value = declaration.partition(":")
            prop = prop.strip().lower()
            value = value.strip()
            if prop not in _ALLOWED_CSS_PROPERTIES:
                continue
            if not value or _DANGEROUS_CSS_RE.search(value):
                continue
            if not _urls_are_safe(value):
                continue
            # 去掉可能用于绕过的反斜杠转义
            value = value.replace("\\", "")
            kept.append(f"{prop}: {value}")
        return "; ".join(kept)


def _urls_are_safe(value: str) -> bool:
    """``url(...)`` 只允许 http(s) 或站内相对路径，禁止 javascript:/data:。"""
    for match in _CSS_URL_RE.finditer(value):
        target = match.group(1).strip().lower()
        if target.startswith(("http://", "https://", "/", "#")):
            continue
        return False
    return True


_CSS_SANITIZER = _InlineStyleSanitizer()


# ------------------------------------------------------------------ 预处理
def _drop_dangerous_elements(html: str) -> str:
    """连同内容一起删掉 script/style 等「正文即代码」的标签。"""
    cleaned = _DROP_ELEMENT_RE.sub("", html)
    cleaned = _DROP_UNCLOSED_RE.sub("", cleaned)
    return _DROP_STRAY_TAG_RE.sub("", cleaned)


def _protect_cid_images(html: str) -> str:
    """把 ``img[src=cid:...]`` 暂存到 ``data-cid``。

    ``cid:`` 不在 §6.2 的协议白名单里，直接交给 bleach 会被剥掉；先挪到
    ``data-cid`` 中转，净化结束后再还原，从而做到「cid: 内联图片不加占位」。
    """

    def rewrite_tag(match: re.Match) -> str:
        tag = match.group(0)
        src_match = _SRC_ATTR_RE.search(tag)
        if not src_match:
            return tag
        value = next((g for g in src_match.groups() if g is not None), "")
        if not value.strip().lower().startswith("cid:"):
            return tag
        # 值里出现引号/尖括号说明结构可疑，放弃保护，交给 bleach 处理
        if any(ch in value for ch in ('"', "'", "<", ">")):
            return tag
        without_src = _SRC_ATTR_RE.sub("", tag, count=1)
        close = without_src.rfind(">")
        if close == -1:
            return tag
        return f'{without_src[:close]} data-cid="{value}"{without_src[close:]}'

    return _IMG_TAG_RE.sub(rewrite_tag, html)


# ------------------------------------------------------------------ 后处理
def _parse_attrs(tag: str) -> dict[str, str]:
    return {name.lower(): value for name, value in _ATTR_RE.findall(tag)}


def _rewrite_img_tag(match: re.Match) -> str:
    """对 bleached 后的 ``<img>``：远程图换占位；合法 cid: 还原为 src。"""
    tag = match.group(0)
    attrs = _parse_attrs(tag)
    src = attrs.get("src", "")
    cid = attrs.get("data-cid", "")

    if src and _HTTP_SRC_RE.match(src):
        ordered = [("src", REMOTE_IMAGE_PLACEHOLDER), ("data-src", src)]
        for key in ("alt", "width", "height"):
            if key in attrs:
                ordered.append((key, attrs[key]))
        ordered.append(("class", REMOTE_IMAGE_CLASS))
        ordered.append(("data-remote", "1"))
        body = " ".join(f'{k}="{v}"' for k, v in ordered)
        return f"<img {body}>"

    if cid and _CID_SRC_RE.match(cid):
        ordered = [("src", cid)]
        for key in ("alt", "width", "height"):
            if key in attrs:
                ordered.append((key, attrs[key]))
        body = " ".join(f'{k}="{v}"' for k, v in ordered)
        return f"<img {body}>"

    # 非法的 data-cid（例如攻击者伪造的 javascript:）直接删掉
    if cid:
        ordered = [(k, v) for k, v in attrs.items() if k != "data-cid"]
        if not ordered:
            return "<img>"
        body = " ".join(f'{k}="{v}"' for k, v in ordered)
        return f"<img {body}>"

    return tag


def _rewrite_images(html: str) -> str:
    return _IMG_TAG_RE.sub(_rewrite_img_tag, html)


def _strip_empty_style(html: str) -> str:
    """删掉被 CSS 过滤清空后的 ``style=""``，让输出更干净。"""
    return _EMPTY_STYLE_RE.sub("", html)


# ------------------------------------------------------------------ 对外 API
def sanitize_html(html: str) -> str:
    """HTML 白名单净化（开发文档 §6.2）。

    - ``bleach.clean`` 使用 §6.2 的 ``ALLOWED_TAGS`` / ``ALLOWED_ATTRS``，
      ``strip=True``，``protocols=["http", "https", "mailto"]``。
    - 先整体丢弃 ``<script>`` / ``<style>`` 等标签及其正文，防 XSS。
    - 内联 ``style`` 经 :class:`_InlineStyleSanitizer` 过滤，剔除
      ``expression()`` / ``javascript:`` 等危险内容。
    - HTTP(S) 远程图片替换为占位图；``cid:`` 内联图片保持原样。

    :param html: 原始 HTML 片段，允许为 ``None``/空串。
    :return: 可安全嵌入页面的 HTML 字符串。
    """
    if not html:
        return ""
    prepared = _protect_cid_images(_drop_dangerous_elements(str(html)))
    cleaned = bleach.clean(
        prepared,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        strip=True,
        protocols=ALLOWED_PROTOCOLS,
        css_sanitizer=_CSS_SANITIZER,
    )
    return _rewrite_images(_strip_empty_style(cleaned))


def sanitize_text(text: str) -> str:
    """纯文本净化（开发文档 §6.2）。

    剥离全部 HTML 标签后把实体还原为普通字符，返回可直接入库/落库展示的
    纯文本。与 :func:`sanitize_html` 相对：前者产出 HTML，本函数产出纯文本。

    :param text: 任意文本（可能是 HTML 片段），允许为 ``None``/空串。
    :return: 不含标签的纯文本。
    """
    if not text:
        return ""
    stripped = bleach.clean(
        _drop_dangerous_elements(str(text)),
        tags=[],
        attributes={},
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )
    return html_module.unescape(stripped).replace("\x00", "").strip()
