"""The one permanent database for what the site keeps between searches.

Saved leads, the Yes / No baler marks, the call log, the record of each day's
search, the count of today's Yelp calls and recent Yelp results all live here. With DATABASE_URL set (a Postgres URL, e.g.
a free Neon database) that is Postgres; otherwise, for the command line and
local use, a SQLite file in the cache folder. Render's own disk is wiped on
every redeploy, so on Render there is no fallback: without DATABASE_URL
nothing is saved and Yelp is paused (its daily limit could not be kept).
"""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import http

log = logging.getLogger(__name__)

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
    """CREATE TABLE IF NOT EXISTS windows (
        name TEXT PRIMARY KEY, version INTEGER NOT NULL, calls TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS searches (
        day TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, info TEXT NOT NULL)""",
    # Every Yes / No click (what it replaced, so a misclick can be undone exactly).
    """CREATE TABLE IF NOT EXISTS mark_changes (
        id TEXT PRIMARY KEY, uid TEXT NOT NULL, value TEXT NOT NULL, previous TEXT NOT NULL,
        at DOUBLE PRECISION NOT NULL, undone_at DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS mark_changes_by_uid ON mark_changes (uid, at)",
    # Searches that failed outright (they give the day back, but stay in the history).
    """CREATE TABLE IF NOT EXISTS search_failures (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
        info TEXT NOT NULL)""",
    # Calls taken back with Undo (the call itself is deleted), so open pages can
    # notice the change without reloading every call.
    """CREATE TABLE IF NOT EXISTS call_undos (
        id TEXT PRIMARY KEY, uid TEXT NOT NULL, at DOUBLE PRECISION NOT NULL)""",
    # For "what changed since the page last looked" (see the /leads refresh).
    "CREATE INDEX IF NOT EXISTS calls_by_time ON calls (at)",
    "CREATE INDEX IF NOT EXISTS leads_by_last_seen ON leads (last_seen)",
    "CREATE INDEX IF NOT EXISTS mark_changes_by_time ON mark_changes (at)",
    "CREATE INDEX IF NOT EXISTS mark_changes_by_undo ON mark_changes (undone_at)",
    "CREATE INDEX IF NOT EXISTS call_undos_by_time ON call_undos (at)",
]
# Every table, for tests that empty them.
TABLES = ("usage", "cache", "marks", "leads", "calls", "windows", "searches", "mark_changes",
          "search_failures", "call_undos")
# Up to this many ids are looked up by id (in chunks); more read the whole table.
BY_ID_LIMIT = 1000
_CHUNK = 500

_ready = set()
_lock = threading.Lock()
_local = threading.local()      # the connection a web request shares (see scope())


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

    @contextmanager
    def transaction(self):
        """All statements inside commit together, or none do."""
        if self.postgres:
            with self.conn.transaction():
                yield self
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def many(self, sql, rows):
        """Run one statement for many rows, all or nothing."""
        rows = list(rows)
        if not rows:
            return
        if self.postgres:
            with self.conn.transaction(), self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), rows)
        else:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise


def rows_for(db, select, column, ids, order=""):
    """Rows of `select` (a SELECT without WHERE) whose `column` is one of ids.

    A few ids are looked up by id; for most of a big list one read of the whole
    table is cheaper, and the caller filters. `order` is an ORDER BY clause.
    """
    ids = list(ids)
    if len(ids) > BY_ID_LIMIT:
        return db.all(f"{select} {order}")
    rows = []
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        marks = ", ".join("?" * len(chunk))
        rows += db.all(f"{select} WHERE {column} IN ({marks}) {order}", chunk)
    return rows


def open_db():
    """A Db on a new connection (autocommit); close it with db.conn.close().

    Raises Unavailable when there is no database.
    """
    url = database_url()
    if url:
        import psycopg
        try:
            conn = psycopg.connect(url, autocommit=True, connect_timeout=15)
        except (psycopg.Error, OSError) as exc:
            log.warning("Database connection failed: %s", exc.__class__.__name__)
            raise Unavailable("the database could not be reached") from exc
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
        except Exception as exc:
            # Two processes creating the same table at once: one of them fails; retry.
            if attempt == 2:
                raise
            log.info("Creating the tables failed (%s); retrying", exc.__class__.__name__)


@contextmanager
def connect():
    """A connection (autocommit) for a few statements; raises Unavailable when there
    is no database. Inside scope() every connect() shares one connection."""
    shared = getattr(_local, "scope", None)
    if shared is not None:
        if shared["db"] is None:
            shared["db"] = open_db()
            shared["opened"] += 1
        yield shared["db"]
        return
    db = open_db()
    try:
        yield db
    finally:
        db.conn.close()


def begin_scope():
    """From now on this thread's connect() calls share one connection (opened when
    first needed) until end_scope(): one connection per web request."""
    _local.scope = {"db": None, "opened": 0}


def end_scope():
    """Close the shared connection; returns how many were opened (0 or 1)."""
    shared = getattr(_local, "scope", None)
    _local.scope = None
    if not shared:
        return 0
    if shared["db"] is not None:
        try:
            shared["db"].conn.close()
        except Exception as exc:  # noqa: BLE001 - sqlite3 or psycopg; the request is done
            log.warning("Closing the database connection failed: %s", exc)
    return shared["opened"]


@contextmanager
def scope():
    begin_scope()
    try:
        yield
    finally:
        end_scope()
