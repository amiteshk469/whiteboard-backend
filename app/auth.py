import os
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from .db import users

JWT_SECRET = os.environ["JWT_SECRET"]
if len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET must be at least 32 characters")
JWT_ALGORITHM = "HS256"
JWT_TTL_MINUTES = int(os.getenv("JWT_TTL_MINUTES", "10080"))

passwords = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)


def create_token(user_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + timedelta(minutes=JWT_TTL_MINUTES)},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def token_user_id(token: str) -> str | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])["sub"]
    except (InvalidTokenError, KeyError, TypeError):
        return None


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


async def current_user(credentials: Credentials):
    user_id = token_user_id(credentials.credentials) if credentials else None
    user = await users.find_one({"_id": user_id}) if user_id else None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return user


def public_user(user: dict) -> dict:
    return {"id": user["_id"], "name": user["name"], "email": user["email"]}
