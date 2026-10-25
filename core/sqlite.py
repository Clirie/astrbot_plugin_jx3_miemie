# pyright: reportOptionalMemberAccess=false
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
import aiosqlite

class AsyncSQLiteDB:
    def __init__(self, db_path: str = "data.db"):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._transaction_owner = None
        self._savepoint = 0

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    @asynccontextmanager
    async def _guard(self):
        if self._transaction_owner is asyncio.current_task():
            yield
        else:
            async with self._lock:
                yield

    async def connect(self):
        async with self._guard():
            if self.conn is not None:
                return
            self.conn = await aiosqlite.connect(self.db_path)
            self.conn.row_factory = aiosqlite.Row
            await self.conn.execute("PRAGMA busy_timeout=5000")
            await self.conn.execute("PRAGMA foreign_keys=ON")

    async def close(self):
        async with self._guard():
            if self.conn:
                await self.conn.close()
                self.conn = None

    @asynccontextmanager
    async def transaction(self):
        async with self._guard():
            nested = self._transaction_owner is asyncio.current_task()
            self._savepoint += 1
            name = f"jx3_tx_{self._savepoint}"
            async with self.conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE"):
                pass
            if not nested:
                self._transaction_owner = asyncio.current_task()
            try:
                yield self
                if nested:
                    async with self.conn.execute(f"RELEASE SAVEPOINT {name}"):
                        pass
                else:
                    await self.conn.commit()
            except BaseException:
                if nested:
                    await self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    await self.conn.execute(f"RELEASE SAVEPOINT {name}")
                else:
                    await self.conn.rollback()
                raise
            finally:
                if not nested:
                    self._transaction_owner = None

    async def execute(self, sql: str, params: Tuple = ()):
        await self.execute_affected(sql, params)

    async def _write(self, sql, params, result):
        async with self._guard():
            owned = self._transaction_owner is asyncio.current_task()
            try:
                async with self.conn.execute(sql, params) as cursor:
                    value = getattr(cursor, result)
                if not owned:
                    await self.conn.commit()
                return value
            except BaseException:
                if not owned:
                    await self.conn.rollback()
                raise

    async def execute_affected(self, sql: str, params: Tuple = ()) -> int:
        return await self._write(sql, params, "rowcount")

    async def execute_insert(self, sql: str, params: Tuple = ()) -> int:
        return await self._write(sql, params, "lastrowid")

    async def execute_transaction(self, statements: List[Tuple[str, Tuple[Any, ...]]]):
        async with self.transaction():
            for sql, params in statements:
                await self.execute(sql, params)

    async def fetch_one(self, sql: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        async with self._guard():
            async with self.conn.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def fetch_all(self, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        async with self._guard():
            async with self.conn.execute(sql, params) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    # ======================
    # CRUD
    # ======================

    async def insert(self, table: str, data: Dict[str, Any]):
        keys = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        sql = f"INSERT INTO {table} ({keys}) VALUES ({placeholders})"
        await self.execute(sql, tuple(data.values()))

    async def update(self, table: str, data: Dict[str, Any], where: str, params: Tuple):
        set_clause = ", ".join([f"{k}=?" for k in data.keys()])
        sql = f"UPDATE {table} SET {set_clause} WHERE {where}"
        await self.execute(sql, tuple(data.values()) + params)

    async def delete(self, table: str, where: str, params: Tuple):
        sql = f"DELETE FROM {table} WHERE {where}"
        await self.execute(sql, params)

    async def select_one(self, table: str, where: str = "", params: Tuple = ()):
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return await self.fetch_one(sql, params)

    async def select_all(self, table: str, where: str = "", params: Tuple = ()):
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return await self.fetch_all(sql, params)
