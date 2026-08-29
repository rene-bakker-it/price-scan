from collections.abc import Iterable

from aiosqlite import Connection

name_queries: dict[str, str] = {
    'tables': "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
}

async def list_names(db: Connection, query_name: str, parameters: Iterable[str] | None = None) -> list[str]:
    cursor = await db.execute(name_queries[query_name], parameters)
    rows = await cursor.fetchall()
    return [row['name'] for row in rows]
