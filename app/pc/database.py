import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger('pc.database')

# Directory bundled with the source code; always holds the schema (tables.sql).
CODE_RESOURCES_DIR = Path(__file__).parent.parent / 'resources'
# Directory where the database lives. Overridable via the RESOURCE_DIR
# environment variable so it can be pointed at a persistent volume.
RESOURCES_DIR = Path(os.environ.get('RESOURCE_DIR', CODE_RESOURCES_DIR))
TABLES_SQL = CODE_RESOURCES_DIR / 'tables.sql'
DB_PATH = RESOURCES_DIR / 'data.db'
DB_PATH_TEST = RESOURCES_DIR / 'data_test.db'

async def create_database(db_path: Path):
    logger.warning(f'Creating database: {db_path}.')
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with open(TABLES_SQL) as fp:
        sql = fp.read()
    async with aiosqlite.connect(f'file:{db_path}', uri=True) as db:
        await db.executescript(sql)
        await db.commit()

@asynccontextmanager
async def get_db(read_only = True, db_path: Path | None = None) -> AsyncGenerator[aiosqlite.Connection, None]:

    if db_path is None:
        db_path = DB_PATH
    if not db_path.exists():
        await create_database(db_path)

    uri = f'file:{db_path}'
    if read_only:
        uri += "?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as db:
        db.row_factory = aiosqlite.Row
        yield db


if __name__ == "__main__":
    import asyncio
    import os

    logging.basicConfig(
        format='%(asctime)s [%(process)d] [%(levelname)s] %(name)-10s %(message)s', datefmt='[%Y-%m-%d %H:%M:%S %z]', level=logging.INFO
    )

    async def test_create_db(name: str = 'test.db'):
        db_name = RESOURCES_DIR / name
        if os.path.exists(db_name):
            logger.warning(f'Database already exists: {db_name}: Removed.')
            os.remove(db_name)
        async with get_db(read_only=True, db_path=db_name) as db:
            sql = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()
            tabs = [row['name'] for row in rows]

            logger.info(f'Tables: {tabs}')

    asyncio.run(test_create_db())
