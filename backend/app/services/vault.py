"""模型 API Key 和 MCP 令牌的密文。

密钥从 SECRET_KEY 的 SHA256 派生 Fernet 密钥，密文只进 secrets 表。
接口响应只返回 has_api_key / has_token，不把明文送回浏览器。
SECRET_KEY 一变，旧密文就解不开。
"""

import base64
import hashlib

from cryptography.fernet import Fernet

from app.security import secret_key


def _fernet() -> Fernet:
    raw = hashlib.sha256(secret_key().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
