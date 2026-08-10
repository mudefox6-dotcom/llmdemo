"""登录相关接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.auth import create_token, require_auth, verify_credentials
from app.core.config import get_settings
from app.core.logger import logger

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class LoginResponse(BaseModel):
    token: str
    username: str
    expires_at: int = Field(..., description="过期时间戳（秒）")


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """账号密码登录，成功返回 token。

    失败时统一返回"账号或密码错误"，不区分是哪一项不对——
    否则会暴露"这个用户名存在"的信息，方便攻击者枚举账号。
    """
    if not verify_credentials(req.username, req.password):
        logger.info("登录失败", extra={"username": req.username[:20]})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号或密码错误",
        )
    token, expires_at = create_token(req.username)
    logger.info("登录成功", extra={"username": req.username})
    return LoginResponse(token=token, username=req.username, expires_at=expires_at)


@router.get("/me")
async def me(username: str = Depends(require_auth)):
    """校验当前 token 是否仍有效。前端启动时用它判断要不要跳登录页。"""
    return {"username": username, "auth_enabled": get_settings().auth_enabled}
