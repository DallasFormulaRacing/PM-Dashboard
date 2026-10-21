from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from models import User


async def upsert_user(session, discord_id, username, name, avatar_url, roles, guilds, is_admin):
    stmt = insert(User).values(
        discord_id=discord_id,
        discord_username=username,
        discord_name=name,
        avatar_url=avatar_url,
        roles=roles,
        guilds=guilds,
        is_admin=is_admin,
        last_login_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.discord_id],
        set_={
            "discord_username": stmt.excluded.discord_username,
            "discord_name": stmt.excluded.discord_name,
            "avatar_url": stmt.excluded.avatar_url,
            "roles": stmt.excluded.roles,
            "guilds": stmt.excluded.guilds,
            "is_admin": stmt.excluded.is_admin,
            "last_login_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.commit()