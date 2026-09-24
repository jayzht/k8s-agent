"""身份认证。

在这之前，`operator` 只是启动时传进来的一个字符串——**谁都能改，谁都能approve**。
审计日志里记的"操作人"因此是自我申报的，不具备任何证明力。
对于一个"写入操作必须人工批准"的系统来说，这是最大的一个洞：
门禁锁得很严，但门卫不看证件。

设计取舍（刻意保持小）
----------------------
- **只用标准库。** 密码用 PBKDF2-HMAC-SHA256，会话用 HMAC 签名。
  不引入 JWT 库、不引入 bcrypt——这个量级不需要，多一个依赖就多一份升级负担。
- **只有两个角色**：``operator`` 能批准写操作，``viewer`` 只能看和问。
  没有权限矩阵、没有策略文件。上一版的教训是：把简单的事情做成可配置的规则，
  最后没人说得清"为什么这个动作被拦了"。
- **会话令牌是自包含的**（用户名+角色+过期时间+签名），服务端不存会话表。
  重启服务不会把所有人踢下线，也不需要额外的存储。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

# PBKDF2 迭代次数。OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议是 600k，
# 但这个界面跑在普通办公机上，每次登录阻塞太久不合适。200k 是折中。
PBKDF2_ITERATIONS = 200_000
SESSION_TTL = 12 * 3600  # 12 小时

ROLE_OPERATOR = "operator"   # 能批准写操作
ROLE_VIEWER = "viewer"       # 只能看和问
ROLES = (ROLE_OPERATOR, ROLE_VIEWER)

ROLE_LABEL = {ROLE_OPERATOR: "运维（可批准变更）", ROLE_VIEWER: "只读（不能批准变更）"}


class AuthError(RuntimeError):
    """认证/授权失败。消息是给人看的，不要泄露"用户名对不对"这种细节。"""


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """返回 ``pbkdf2_sha256$迭代次数$盐$哈希``，可直接存盘。"""
    if not password:
        raise AuthError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, salt.hex(), digest.hex()
    )


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较。

    用 ``hmac.compare_digest`` 而不是 ``==``：后者会在第一个不同字节处提前返回，
    理论上可以从响应时间推断出密码前缀。对这个量级的系统有点过度，
    但成本是零，没有理由不做。
    """
    try:
        algo, iters, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# 用户库
# ---------------------------------------------------------------------------


class UserStore:
    """一个 JSON 文件，存用户名 → 密码哈希 + 角色。

    刻意不做用户管理界面：加人是一条命令，改密码也是一条命令。
    一个只有几个人的运维台不需要一套 RBAC 后台。
    """

    def __init__(self, path: str | Path, users: dict[str, dict[str, str]] | None = None):
        self.path = Path(path)
        self._users: dict[str, dict[str, str]] = users or {}

    # ------------------------------------------------------------------ 读写

    @classmethod
    def load(cls, path: str | Path) -> "UserStore":
        p = Path(path)
        if not p.exists():
            return cls(p, {})
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 文件坏了就当空库——**fail closed**：没有用户 = 谁都进不来，
            # 而不是"读不出来就放行"。
            return cls(p, {})
        return cls(p, dict(data.get("users") or {}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "users": self._users}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # 原子替换，避免写一半被打断留下坏文件
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ 操作

    def add(self, username: str, password: str, role: str = ROLE_OPERATOR) -> None:
        if not username or not username.strip():
            raise AuthError("用户名不能为空")
        if role not in ROLES:
            raise AuthError(f"角色必须是 {' 或 '.join(ROLES)}")
        if len(password) < 8:
            raise AuthError("密码至少 8 位")
        self._users[username.strip()] = {"hash": hash_password(password), "role": role}

    def remove(self, username: str) -> bool:
        return self._users.pop(username, None) is not None

    def set_password(self, username: str, password: str) -> None:
        u = self._users.get(username)
        if u is None:
            raise AuthError(f"用户 {username} 不存在")
        u["hash"] = hash_password(password)

    def verify(self, username: str, password: str) -> dict[str, str] | None:
        """验证成功返回 ``{"username":..., "role":...}``，否则 None。

        用户名不存在时**也要跑一次哈希**，避免"存在的用户响应更慢"这种时间侧信道。
        """
        u = self._users.get(username)
        if u is None:
            hash_password(password)  # 白跑一次，抹平时间差
            return None
        if not verify_password(password, u.get("hash", "")):
            return None
        role = u.get("role", ROLE_OPERATOR)
        return {"username": username, "role": role if role in ROLES else ROLE_VIEWER}

    @property
    def usernames(self) -> list[str]:
        return sorted(self._users)

    def __len__(self) -> int:
        return len(self._users)

    def to_dict(self) -> dict[str, Any]:
        """给界面用的（**不含哈希**）。"""
        return {
            "count": len(self._users),
            "users": [{"username": u, "role": d.get("role", ROLE_OPERATOR)}
                      for u, d in sorted(self._users.items())],
        }


# ---------------------------------------------------------------------------
# 会话令牌
# ---------------------------------------------------------------------------


def load_secret(path: str | Path) -> bytes:
    """读取（或首次生成）用于签名会话的密钥。

    存在文件里而不是写死在代码里——写死的密钥等于没有签名。
    """
    p = Path(path)
    if p.exists():
        raw = p.read_bytes().strip()
        if raw:
            return raw
    p.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    p.write_bytes(secret)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return secret


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(username: str, role: str, secret: bytes,
               *, ttl: int = SESSION_TTL, now: float | None = None) -> str:
    """生成 ``载荷.签名`` 形式的自包含令牌。"""
    payload = {
        "u": username,
        "r": role,
        "exp": int((now or time.time()) + ttl),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_token(token: str, secret: bytes, *, now: float | None = None) -> dict[str, str] | None:
    """校验令牌。签名不对、格式不对、过期，一律返回 None。

    这里**不做任何容错**：任何异常路径都是"拒绝"，没有"降级放行"。
    """
    try:
        body, sig_text = token.split(".", 1)
        expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(sig_text)):
            return None
        payload = json.loads(_b64d(body).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(now or time.time()):
            return None
        role = payload.get("r", "")
        if role not in ROLES:
            return None
        return {"username": str(payload.get("u", "")), "role": role}
    except Exception:  # noqa: BLE001
        return None


def can_approve(role: str) -> bool:
    """只有 operator 能批准写操作。viewer 能看、能问，但按不了那个按钮。"""
    return role == ROLE_OPERATOR
