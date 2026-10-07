import asyncio
import db


async def main():
    await db.connect()
    row = await db.pool.fetchrow("SELECT current_database(), count(*) FROM users")
    print(row)
    await db.disconnect()


asyncio.run(main())