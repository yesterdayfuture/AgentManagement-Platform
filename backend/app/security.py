"""登录口令、访问令牌和外部回调签名。

口令用 PBKDF2，不存明文。访问令牌是自签的 HMAC，载荷里有 sub 和 exp，
不依赖 PyJWT。模型密钥和 MCP 令牌不走这里，它们进 vault，以密文落在 secrets 表。
SECRET_KEY / SSO_SECRET / CHANNEL_SECRET 都有开发默认值，生产环境必须换成环境变量。
"""

import base64
import hashlib
import hmac
import json
import os
import time


def secret_key() -> str:
    return os.environ.get("SECRET_KEY", "limen-dev-secret")


def sso_secret() -> str:
    return os.environ.get("SSO_SECRET", "limen-sso-secret")


def channel_secret() -> str:
    return os.environ.get("CHANNEL_SECRET", "limen-channel-secret")


def hash_password(password: str) -> str:
    """每条口令单独盐。存成「盐的十六进制:摘要的十六进制」。"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 20_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split(":", 1)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 20_000)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sign(raw: str) -> str:
    return hmac.new(secret_key().encode(), raw.encode(), hashlib.sha256).hexdigest()


def issue_token(payload: dict, ttl: int = 86_400) -> str:
    """签发访问令牌。默认一天过期。格式是「去掉补位的 base64url 载荷.HMAC」。"""
    body = {**payload, "exp": int(time.time()) + ttl}
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{raw}.{_sign(raw)}"


def read_token(token: str) -> dict:
    try:
        raw, signature = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("令牌格式无效") from exc
    if not hmac.compare_digest(_sign(raw), signature):
        raise ValueError("令牌签名无效")
    padded = raw + "=" * (-len(raw) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded))
    if int(data.get("exp", 0)) < time.time():
        raise ValueError("令牌已过期")
    return data


def sign_sso(external_id: str, email: str, space_key: str, ts: int) -> str:
    message = f"{external_id}.{email}.{space_key}.{ts}"
    return hmac.new(sso_secret().encode(), message.encode(), hashlib.sha256).hexdigest()
