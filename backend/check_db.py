import asyncio

from sqlalchemy import text

from db import SessionLocal, engine


async def main():
    async with SessionLocal() as session:
        result = await session.execute(
            text("SELECT current_database(), count(*) FROM users")
        )
        print(result.one())
    await engine.dispose()


asyncio.run(main())