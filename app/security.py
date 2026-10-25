"""会话令牌与邀请码。

这一版没有访客密码，也没有「用户自填上游 Key」，所以 V2 里的 PBKDF2 派生、
AES-GCM 字段加密、临时密码生成全部删掉了——留着是死代码，还会拖进
cryptography 这个编译依赖。管理员口令走 hmac.compare_digest 直接比对环境变量。
"""
from __future__ import annotations

import hashlib
import secrets


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_session_token() -> tuple[str, str]:
    """返回 (明文令牌, 哈希)。库里只存哈希，Cookie 里才是明文。"""
    token = secrets.token_urlsafe(32)
    return token, sha256_hex(token)


def new_invite_code(length: int = 8) -> str:
    # 去掉 0/O/1/I 这类看错的字符，邀请码经常要口头或截图转达
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
