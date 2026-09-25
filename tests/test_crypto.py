"""凭据加密与附件存储的安全用例（开发文档 §10.3）。"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from django.test import override_settings

from apps.core.crypto import CredentialError, decrypt_secret, encrypt_secret, mask_secret
from apps.mailboxes import storage


def test_encrypt_decrypt_roundtrip(db):
    token = encrypt_secret("飞书授权码-abc123")
    assert isinstance(token, bytes)
    assert b"abc123" not in token  # 密文中不得出现明文
    assert decrypt_secret(token) == "飞书授权码-abc123"


def test_decrypt_with_wrong_key_fails(db):
    token = encrypt_secret("secret-value")
    with override_settings(FERNET_KEY=Fernet.generate_key().decode()):
        with pytest.raises(CredentialError):
            decrypt_secret(token)


def test_decrypt_empty_token_fails(db):
    with pytest.raises(CredentialError):
        decrypt_secret(b"")


def test_mask_secret_never_leaks_plaintext():
    masked = mask_secret("abcdefgh")
    assert "abcdefgh" not in masked
    assert masked.startswith("ab")


def test_mailbox_secret_is_encrypted_at_rest(unified_mailbox):
    unified_mailbox.set_secret("plain-text-password")
    unified_mailbox.save()
    unified_mailbox.refresh_from_db()

    raw = bytes(unified_mailbox.secret_encrypted)
    assert b"plain-text-password" not in raw
    assert unified_mailbox.get_secret() == "plain-text-password"


def test_attachment_path_rejects_traversal(media_root):
    with pytest.raises(ValueError):
        storage.absolute_path("../../etc/passwd")
    with pytest.raises(ValueError):
        storage.absolute_path("/etc/passwd")


def test_attachment_filename_is_sanitized(media_root):
    rel = storage.attachment_rel_path(1, "msg-1", "../../../evil.sh")
    assert ".." not in rel
    assert rel.startswith("attachments/1/")
    path = storage.save_file(rel, b"data")
    assert (media_root / path).exists()
    assert media_root / "attachments" / "1" / "msg-1" / "evil.sh" == media_root / path


def test_store_inbound_attachment_enforces_size_limit(media_root):
    record = storage.store_inbound_attachment(
        ticket_id=1,
        message_key="msg-1",
        filename="big.bin",
        content=b"x" * 100,
        limit_bytes=10,
    )
    assert record is None
    assert not (media_root / "attachments").exists()


@pytest.fixture
def media_root(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path
