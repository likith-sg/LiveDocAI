from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def create_tables():
    from app.models import APILog, Endpoint, Documentation, DocHistory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Add columns that may not exist in older deployments
    async with AsyncSessionLocal() as db:
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS api_key VARCHAR(100)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_id VARCHAR(50)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_token TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username VARCHAR(100)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)",
            "ALTER TABLE api_logs ADD COLUMN IF NOT EXISTS user_id VARCHAR(36)",
            "ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS user_id VARCHAR(36)",
        ]
        for sql in migrations:
            try:
                from sqlalchemy import text
                await db.execute(text(sql))
                await db.commit()
            except Exception:
                await db.rollback()
