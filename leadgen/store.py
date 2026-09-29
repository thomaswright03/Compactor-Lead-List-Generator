"""The one permanent database for what the site keeps between searches.

Saved leads, the Yes / No baler marks, the call log, the record of each day's
search, the count of today's Yelp calls and recent Yelp results all live here. With DATABASE_URL set (a Postgres URL, e.g.
a free Neon database) that is Postgres; otherwise, for the command line and
local use, a SQLite file in the cache folder. Render's own disk is wiped on
every redeploy, so on Render there is no fallback: without DATABASE_URL
nothing is saved and Yelp is paused (its daily limit could not be kept).
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

from . import http

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS usage (
        name TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL,
        PRIMARY KEY (name, day))""",
    """CREATE TABLE IF NOT EXISTS cache (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS marks (
        uid TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS leads (
        uid TEXT PRIMARY KEY, lead TEXT NOT NULL, parts TEXT NOT NULL, ids TEXT NOT NULL,
        first_seen DOUBLE PRECISION NOT NULL, last_seen DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS calls (
        id TEXT PRIMARY KEY, uid TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
        outcome TEXT NOT NULL, notes TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS calls_by_uid ON calls (uid)",
    """CREATE TABLE IF NOT EXISTS searches (
        day TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, info TEXT NOT NULL)""",
]

_ready = set()
_lock = threading.Lock()


class Unavailable(RuntimeError):
    """No database to use; the message says why, for the page."""


def on_render():
    return bool(os.environ.get("RENDER"))


def database_url():
    return os.environ.get("DATABASE_URL", "").strip()


class Db:
    def __init__(self, conn, postgres):
        self.conn = conn
        self.postgres = postgres

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.postgres else sql

    def run(self, sql, params=()):
        self.conn.execute(self._sql(sql), params)

    def all(self, sql, params=()):
        return list(self.conn.execute(self._sql(sql), params).fetchall())

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def many(self, sql, rows):
        """Run one statement for many rows, all or nothing."""
        rows = list(rows)
        if not rows:
            return
        if self.postgres:
            with self.conn.transaction():
                with self.conn.cursor() as cur:
                    cur.executemany(self._sql(sql), rows)
        else:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise


def open_db():
    """A Db on a new connection (autocommit); close it with db.conn.close().

    Raises Unavailable when there is no database.
    """
    url = database_url()
    if url:
        import psycopg
        try:
            conn = psycopg.connect(url, autocommit=True, connect_timeout=15)
        except Exception as exc:
            raise Unavailable(f"the database could not be reached ({exc.__class__.__name__})")
        db, ready_key = Db(conn, True), url
    elif on_render():
        raise Unavailable("no database is connected yet (DATABASE_URL is not set in Render)")
    else:
        http.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = http.CACHE_DIR / "leadgen.db"
        conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        db, ready_key = Db(conn, False), str(path.resolve())
    try:
        with _lock:
            if ready_key not in _ready or not db.postgres:
                _create_tables(db)
                if db.postgres:
                    _ready.add(ready_key)
    except Exception:
        conn.close()
        raise
    return db


def _create_tables(db):
    for attempt in range(3):
        try:
            for statement in SCHEMA:
                db.run(statement)
            return
        except Exception:
            # Two processes creating the same table at once: one of them fails; retry.
            if attempt == 2:
                raise


@contextmanager
def connect():
    """A connection (autocommit) for a few statements; raises Unavailable when there
    is no database."""
    db = open_db()
    try:
        yield db
    finally:
        db.conn.close()
