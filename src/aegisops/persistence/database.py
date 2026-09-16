from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aegisops.persistence.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def sqlite_settings(connection: object, _: object) -> None:
                cursor = connection.cursor()  # type: ignore[attr-defined]
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=10000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

    async def create_for_test(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()


async def db_now(session: AsyncSession) -> float:
    # The database clock keeps lease decisions consistent across worker hosts.
    sql = "SELECT EXTRACT(EPOCH FROM clock_timestamp())"
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        sql = "SELECT (julianday('now') - 2440587.5) * 86400.0"
    return float((await session.execute(text(sql))).scalar_one())
