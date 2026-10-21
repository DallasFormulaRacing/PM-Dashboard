from datetime import datetime, timedelta, timezone

import jwt

from config import settings

ALGORITHM = "HS256"
EXPIRE_HOURS = 3


def create_access_token(discord_id: str, is_admin: bool) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": discord_id,
        "is_admin": is_admin,
        "iat": now,
        "exp": now + timedelta(hours=EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])