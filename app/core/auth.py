"""登录认证：账号密码校验 + HMAC 签名 token。

为什么不用 JWT 库：只需要"签发一个带过期时间、不可伪造的字符串"，
标准库的 hmac + base64 就够了，不必为演示项目引入额外依赖。

为什么不在服务端存 session：签名 token 是**无状态**的——重启进程、多 worker
并发都不受影响；而内存 session 一重启就全部失效（本项目常改代码热重载）。

token 结构：base64url(用户名.过期时间戳) + "." + HMAC-SHA256 签名
校验时重算签名并比对（用 compare_digest 防时序攻击），再检查是否过期。
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

from fastapi import Header, HTTPException, Query, status

from app.core.config import get_settings
from app.core.logger import logger


def _sign(payload: str) -> str:
    secret = get_settings().auth_secret.encode("utf-8")
    digest = hmac.new(secret, payload.encode("utf-8"), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(data: str) -> str:
    # base64 要求长度是 4 的倍数，签发时去掉的 '=' 这里补回来
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def verify_credentials(username: str, password: str) -> bool:
    """校验账号密码。用 compare_digest 而非 == ，避免按字符比较泄露长度/内容信息。"""
    settings = get_settings()
    ok_user = hmac.compare_digest(username or "", settings.auth_username)
    ok_pass = hmac.compare_digest(password or "", settings.auth_password)
    return ok_user and ok_pass


def create_token(username: str) -> tuple[str, int]:
    """签发 token，返回 (token, 过期时间戳)。"""
    settings = get_settings()
    expires_at = int(time.time()) + settings.auth_token_ttl_hours * 3600
    payload = _b64(f"{username}.{expires_at}")
    return f"{payload}.{_sign(payload)}", expires_at


def parse_token(token: str) -> str | None:
    """校验 token 并返回用户名；无效或过期返回 None。"""
    if not token or "." not in token:
        return None
    payload, _, signature = token.rpartition(".")
    if not payload or not signature:
        return None
    # 先验签名再解内容——签名不对就不必信任里面的任何字节
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        username, _, expires_raw = _unb64(payload).rpartition(".")
        if int(expires_raw) < int(time.time()):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return username or None


def _bearer(authorization: str) -> str:
    """从 `Authorization: Bearer xxx` 里取出 token，格式不符返回空串。"""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _authorize(token: str) -> str:
    """校验 token 并返回用户名，失败抛 401。认证关闭时直接放行。"""
    settings = get_settings()
    if not settings.auth_enabled:
        return settings.auth_username

    username = parse_token(token)
    if username is None:
        logger.info("认证失败：token 缺失或已过期")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return username


async def require_auth(authorization: str = Header(default="")) -> str:
    """FastAPI 依赖：校验 Authorization 头，返回登录用户名。

    认证关闭时（AUTH_ENABLED=false）直接放行，方便本地调试与自动化测试。
    """
    return _authorize(_bearer(authorization))


async def require_auth_or_query_token(
    authorization: str = Header(default=""),
    token: str = Query(default="", description="仅供 SSE 使用的 token"),
) -> str:
    """同 require_auth，但额外允许把 token 放在 ?token= 查询参数里。

    为什么需要这个"开后门"的版本：浏览器的 EventSource（SSE 客户端）**不支持
    自定义请求头**，拿不到地方放 Authorization。业界常见做法有三种——
    改用 fetch 手写流解析、放 Cookie、放 query 参数；这里选 query 参数，
    因为改动最小且不引入 CSRF 面（Cookie 会被浏览器自动带上，反而要额外防护）。

    代价是 token 会出现在 nginx access log 里。因此只给 /stream 这一个路由用，
    并且 token 有 12 小时有效期（AUTH_TOKEN_TTL_HOURS）来限制泄露后的影响窗口。
    """
    return _authorize(_bearer(authorization) or token.strip())
