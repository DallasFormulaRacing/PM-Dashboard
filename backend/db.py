import asyncpg
from config import settings

pool = None


async def connect():
    global pool
    pool = await asyncpg.create_pool(settings.database_url)


async def disconnect():
    await pool.close()