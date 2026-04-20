from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import aiosqlite

from .config import settings

_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: str) -> str:
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not configured in .env")
    exp = datetime.now(timezone.utc) + timedelta(days=settings.jwt_expiry_days)
    return jwt.encode({"sub": user_id, "exp": exp}, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> str:
    if not settings.jwt_secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def _get_db_for_auth():
    from .database import get_db as _raw
    conn = await _raw()
    try:
        yield conn
    finally:
        await conn.close()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn: aiosqlite.Connection = Depends(_get_db_for_auth),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = decode_token(credentials.credentials)
    async with conn.execute(
        "SELECT id, username, created_at FROM users WHERE id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}


CurrentUser = Annotated[dict, Depends(get_current_user)]
