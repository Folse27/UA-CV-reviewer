"""One table, no migrations. Schema is created on startup.

psycopg3 rather than asyncpg: it ships binary wheels for current Pythons,
so the project installs on 3.12 and 3.14 alike with no C compiler.
"""
import logging

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from config import CV_CHARS, DATABASE_URL, SESSION_DAYS

log = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  chat_id      BIGINT PRIMARY KEY,
  chat_summary TEXT NOT NULL DEFAULT '',
  current_cv   TEXT NOT NULL DEFAULT '',
  lang         TEXT NOT NULL DEFAULT 'uk',
  status       TEXT NOT NULL DEFAULT 'ok',
  offtopic     INT  NOT NULL DEFAULT 0,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

DEFAULTS = {"chat_summary": "", "current_cv": "", "lang": "uk", "status": "ok", "offtopic": 0}


async def init() -> None:
    global _pool
    _pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=False)
    await _pool.open(wait=True)
    async with _pool.connection() as c:
        await c.execute(SCHEMA)
    await purge()


async def close() -> None:
    if _pool:
        await _pool.close()


async def purge() -> int:
    """Blocked users survive the purge — expiring their row would silently unban them."""
    async with _pool.connection() as c:
        cur = await c.execute(
            "DELETE FROM sessions WHERE updated_at < now() - make_interval(days => %s)"
            " AND status <> 'blocked'",
            (SESSION_DAYS,),
        )
    if cur.rowcount:
        log.info("purged %d sessions older than %d days", cur.rowcount, SESSION_DAYS)
    return cur.rowcount


async def load(chat_id: int) -> dict:
    async with _pool.connection() as c:
        cur = await c.cursor(row_factory=dict_row).execute(
            "SELECT chat_summary, current_cv, lang, status, offtopic FROM sessions WHERE chat_id=%s",
            (chat_id,),
        )
        row = await cur.fetchone()
    return row or dict(DEFAULTS)


async def save(chat_id: int, **fields) -> None:
    s = dict(DEFAULTS) | (await load(chat_id)) | fields
    async with _pool.connection() as c:
        await c.execute(
            """INSERT INTO sessions (chat_id, chat_summary, current_cv, lang, status, offtopic, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (chat_id) DO UPDATE SET
                 chat_summary=EXCLUDED.chat_summary, current_cv=EXCLUDED.current_cv,
                 lang=EXCLUDED.lang, status=EXCLUDED.status, offtopic=EXCLUDED.offtopic,
                 updated_at=now()""",
            (
                chat_id,
                s["chat_summary"][:4000],
                s["current_cv"][:CV_CHARS],
                s["lang"],
                s["status"],
                s["offtopic"],
            ),
        )
