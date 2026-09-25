"""邮箱凭据对称加密（Fernet，开发文档 §4.2 / §9.3）。

- 明文只存在于内存中，绝不写日志（见 mask_secret）。
- FERNET_KEY 来自环境变量；丢失后所有邮箱凭据需重新录入。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialError(Exception):
    """凭据加解密失败。"""


def _fernet() -> Fernet:
    key = getattr(settings, "FERNET_KEY", None)
    if not key:
        raise CredentialError(
            "FERNET_KEY 未配置：请在环境变量中设置 Fernet 密钥后重试。"
        )
    key_bytes = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    try:
        return Fernet(key_bytes)
    except (ValueError, TypeError) as exc:  # pragma: no cover - 配置错误
        raise CredentialError("FERNET_KEY 格式非法，应为 urlsafe base64 的 32 字节密钥。") from exc


def encrypt_secret(plaintext: str) -> bytes:
    """加密明文凭据，返回可直接存入 BinaryField 的字节串。"""
    if plaintext is None:
        raise CredentialError("待加密的凭据不能为空。")
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(token: bytes | memoryview | None) -> str:
    """解密 BinaryField 中的凭据。"""
    if not token:
        raise CredentialError("邮箱未配置凭据。")
    raw = bytes(token)
    try:
        return _fernet().decrypt(raw).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialError(
            "凭据解密失败：FERNET_KEY 可能已更换或数据损坏。"
        ) from exc


def mask_secret(value: str | None, keep: int = 2) -> str:
    """日志用掩码，永不输出明文。"""
    if not value:
        return "***"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * max(4, len(value) - keep)


def development_key() -> str:
    """从固定种子派生开发/测试用密钥（生产禁止使用）。"""
    return base64.urlsafe_b64encode(hashlib.sha256(b"dev-fernet").digest()).decode()
