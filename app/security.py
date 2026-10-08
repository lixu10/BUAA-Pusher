from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return _hasher.verify(encoded, password)
    except (VerifyMismatchError, InvalidHash):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CredentialVault:
    """AES-GCM envelope for school credentials; ciphertext is bound to a user id."""

    def __init__(self, data_dir: Path, configured_key: str = ""):
        self.key = self._load_key(data_dir, configured_key)
        self.aes = AESGCM(self.key)

    @staticmethod
    def _load_key(data_dir: Path, configured_key: str) -> bytes:
        if configured_key:
            try:
                key = base64.urlsafe_b64decode(configured_key.encode("ascii"))
            except Exception as exc:
                raise RuntimeError("PUAA_MASTER_KEY 必须是 URL-safe Base64") from exc
            if len(key) != 32:
                raise RuntimeError("PUAA_MASTER_KEY 解码后必须为 32 字节")
            return key
        data_dir.mkdir(parents=True, exist_ok=True)
        key_file = data_dir / ".master_key"
        if key_file.exists():
            return base64.urlsafe_b64decode(key_file.read_bytes())
        key = AESGCM.generate_key(bit_length=256)
        key_file.write_bytes(base64.urlsafe_b64encode(key))
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        return key

    def encrypt(self, user_id: int, plaintext: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self.aes.encrypt(nonce, plaintext.encode("utf-8"), str(user_id).encode())
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, user_id: int, encoded: str) -> str:
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return self.aes.decrypt(payload[:12], payload[12:], str(user_id).encode()).decode("utf-8")
