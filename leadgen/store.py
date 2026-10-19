"""The one permanent database for what the site keeps between searches.

Saved leads, the Yes / No baler marks, the call log, the record of each day's
search, the site's recent problems, the count of today's Yelp calls and recent
Yelp results all live here. With DATABASE_URL set (a Postgres URL, e.g. a free
Neon database) that is Postgres; otherwise, for the command line and
local use, a SQLite file in the cache folder. Render's own disk is wiped on
every redeploy, so on Render there is no fallback: without DATABASE_URL
nothing is saved and Yelp is paused (its daily limit could not be kept).
"""

import atexit
import contextvars
import functools
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

# Imported here, while the site's code loads, never first inside a request or the wake-up
# thread: two threads importing psycopg at the same moment can leave one of them with a
# half-loaded module ("partially initialized module 'psycopg' has no attribute 'connect'"),
# and then no connection could be made until the site restarted.
import psycopg
from psycopg.pq import TransactionStatus

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
    # Failed searches and logged errors, for the Find leads page's "problems" line (alerts.py).
    """CREATE TABLE IF NOT EXISTS problems (
        id TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, kind TEXT NOT NULL,
        text TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS problems_by_time ON problems (at)",
    # Who made each Yes / No click and each call: the name set in that browser, by the
    # mark_changes or calls row's id. A table of its own, so no existing table changes;
    # clicks and calls from before it (or with no name set) simply have no row here.
    """CREATE TABLE IF NOT EXISTS made_by (
        id TEXT PRIMARY KEY, name TEXT NOT NULL)""",
    # The emergency switches flipped inside the site (switches.py): 1 = on.
    """CREATE TABLE IF NOT EXISTS switches (
        name TEXT PRIMARY KEY, value INTEGER NOT NULL, by_name TEXT,
        at DOUBLE PRECISION)""",
    # Saved leads merged into another because they were parts of one site (saved.py
    # merge_sites): the merged row's id, the lead it joined, when, and the row as it
    # was then (with its mark), so nothing about it is lost.
    """CREATE TABLE IF NOT EXISTS merged_leads (
        uid TEXT PRIMARY KEY, into_uid TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
        row TEXT NOT NULL)""",
    # Saved leads taken out of the list on request because they are far outside AARCO's
    # area (cleanup.py, `python -m leadgen out-of-area --remove`): the row as it was, so
    # `--restore` can put it back. Searches never remove leads.
    """CREATE TABLE IF NOT EXISTS removed_leads (
        uid TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, miles DOUBLE PRECISION NOT NULL,
        row TEXT NOT NULL)""",
    # A search while it runs (interrupted.py): when it last said it was alive, and what
    # finishing it needs (where, how far, the search words), so a server restart in the
    # middle of it keeps what it found. Its rows go once the search ends.
    """CREATE TABLE IF NOT EXISTS search_runs (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, started DOUBLE PRECISION NOT NULL,
        alive DOUBLE PRECISION NOT NULL, info TEXT NOT NULL)""",
    # The listings a running search's sources have returned so far, a batch every few
    # seconds (interrupted.py).
    """CREATE TABLE IF NOT EXISTS search_found (
        id TEXT PRIMARY KEY, run TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
        leads TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS search_found_by_run ON search_found (run)",
    # Verified phone numbers and contact names the sales team saved on leads (contacts.py):
    # each save a row of its own, with who saved it and when; a lead's latest row is its
    # current one. Apart from the leads table, so a search never changes them.
    """CREATE TABLE IF NOT EXISTS lead_contacts (
        id TEXT PRIMARY KEY, uid TEXT NOT NULL, phone TEXT NOT NULL, contact TEXT NOT NULL,
        by_name TEXT NOT NULL, at DOUBLE PRECISION NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS lead_contacts_by_uid ON lead_contacts (uid, at)",
    "CREATE INDEX IF NOT EXISTS lead_contacts_by_time ON lead_contacts (at)",
    # Each start of the live site, and which version it ran (awake.py: a start during
    # Utah working hours with no new version means a restart or sleep).
    """CREATE TABLE IF NOT EXISTS site_starts (
        id TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL, version TEXT NOT NULL)""",
    # When each outside source (Yelp, Google, the map data, the place lookup, the alert
    # webhook) last answered and last failed, and why (health.py; /healthz shows it). One
    # row per source and outcome, replaced in place: operating state like `windows` and
    # `switches`, never saved data.
    """CREATE TABLE IF NOT EXISTS source_status (
        source TEXT NOT NULL, outcome TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
        reason TEXT NOT NULL, PRIMARY KEY (source, outcome))""",
    # Calls taken out with "Not called" (calls.withdraw), so the business is a lead that
    # hasn't been called again: each call as it was (with who made it, from made_by), when
    # it was taken out and by whom. Kept for good; the id is also in call_undos, so a late
    # copy of the call never comes back.
    """CREATE TABLE IF NOT EXISTS calls_withdrawn (
        id TEXT PRIMARY KEY, uid TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
        outcome TEXT NOT NULL, notes TEXT NOT NULL, called_by TEXT NOT NULL,
        withdrawn_at DOUBLE PRECISION NOT NULL, withdrawn_by TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS calls_withdrawn_by_uid ON calls_withdrawn (uid)",
    # The company's current customers, kept off the lead lists (customers.py). Only one-way
    # fingerprints of each customer's phone, address and name: the list itself never
    # reaches the site. customer_list holds the site's salt and who put the list on file.
    """CREATE TABLE IF NOT EXISTS customer_list (
        id TEXT PRIMARY KEY, salt TEXT NOT NULL, at DOUBLE PRECISION, by_name TEXT NOT NULL,
        customers INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS customer_keys (
        hash TEXT PRIMARY KEY)""",
]
# Every table, for tests that empty them.
TABLES = ("usage", "cache", "marks", "leads", "calls", "windows", "searches", "mark_changes",
          "search_failures", "call_undos", "problems", "made_by", "switches", "merged_leads",
          "removed_leads", "search_runs", "search_found", "lead_contacts", "site_starts",
          "source_status", "calls_withdrawn", "customer_list", "customer_keys")
# Sign-up mode (signup_mode.py): the accounts, kept in the base database (the one used
# with no company set, see _database), never inside a company's own. Apart from SCHEMA,
# so they aren't created in every company's schema and the backup (backup.py, which
# copies SCHEMA's tables) stays as it was; created when accounts.py first uses them
# (with_accounts), so AARCO's own database never gets them.
# companies: each company that signed up, its own details (company.py reads them) and
# its plan (plan, status: trialing / active / past_due / canceled; trial_ends_at; the
# Stripe ids, set by billing.py). users: who can log in, each in one company
# (role owner or member), the password as werkzeug.security's hash. tokens: the links
# emailed for verifying an email address, resetting a password and joining a company
# (an invite), each kept only as its SHA-256, used once and until it expires.
ACCOUNT_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, short_name TEXT NOT NULL,
        address TEXT NOT NULL DEFAULT '', lat DOUBLE PRECISION, lon DOUBLE PRECISION,
        radius_miles DOUBLE PRECISION NOT NULL, search_words TEXT NOT NULL DEFAULT '[]',
        competitors TEXT NOT NULL DEFAULT '[]', created_at DOUBLE PRECISION NOT NULL,
        plan TEXT NOT NULL, status TEXT NOT NULL, trial_ends_at DOUBLE PRECISION,
        stripe_customer_id TEXT, stripe_subscription_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        name TEXT NOT NULL, company_id TEXT NOT NULL, role TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL, email_verified_at DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS users_by_company ON users (company_id)",
    """CREATE TABLE IF NOT EXISTS tokens (
        hash TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id TEXT, email TEXT NOT NULL,
        company_id TEXT, expires_at DOUBLE PRECISION NOT NULL, used_at DOUBLE PRECISION,
        created_at DOUBLE PRECISION NOT NULL)""",
    # The Stripe events billing.py has handled (by Stripe's event id), so a resent one
    # changes nothing.
    """CREATE TABLE IF NOT EXISTS stripe_events (
        id TEXT PRIMARY KEY, type TEXT NOT NULL, received_at DOUBLE PRECISION NOT NULL)""",
]
ACCOUNT_TABLES = ("companies", "users", "tokens", "stripe_events")

# The company whose data this thread (a web request, or a search it started) works on
# (sign-up mode): its id, or None for the base database (AARCO's own site, the command
# line, and the accounts above). Set for each web request (web/accounts.py) and carried
# into the background threads a request starts (carry).
_company: contextvars.ContextVar[str | None] = contextvars.ContextVar("company", default=None)
# A company's id: what its schema (Postgres) and file (SQLite) are named after.
_COMPANY_ID = re.compile(r"^[a-z0-9]{6,40}$")
# How a company's database key names its schema after the base URL (Postgres).
_SCHEMA_MARK = " |schema="

# Up to this many ids are looked up by id (in chunks); more read the whole table.
BY_ID_LIMIT = 1000
_CHUNK = 500

_ready: set[str] = set()       # the databases whose tables this process has created
# The last failed connection in this process: (when, why), for /healthz (health.py); kept
# in memory, as a database that can't be reached can't record it.
last_failure: tuple[float, str] | None = None
_lock = threading.Lock()
_local = threading.local()      # the connection a web request shares (see scope())

# Connections are kept open between requests and handed out again, so a page load
# doesn't open a new (TLS) connection to Postgres each time. At most POOL_SIZE idle
# ones are kept per database: the web server's threads (render.yaml: --threads 8), so
# every thread can find one waiting. More are opened when more are busy at once
# (a search's background thread as well), and closed when they come back.
POOL_SIZE = 8
# Every idle one is asked "SELECT 1" before it is handed out, however briefly it sat:
# the database may have dropped it (a Neon restart, a network reset) a moment ago. One
# idle longer than POOL_MAX_IDLE is closed instead (Neon closes idle connections after a
# few minutes).
POOL_CHECK_AFTER = 0.0
POOL_MAX_IDLE = 240.0
_pool: dict[str, list[tuple["Db", float]]] = {}
_pool_lock = threading.Lock()


# A row as the database returns it.
Row = tuple[Any, ...]


def person(name: object) -> str:
    """A name as typed in "Your name" (who made a mark or call): one line, at most
    MAX_NAME characters; "" for none."""
    text = " ".join("".join(c for c in str(name or "") if c.isprintable()).split())
    return text[:MAX_NAME]


MAX_NAME = 60


def record_maker(db: "Db", row_id: str, name: str) -> None:
    """Note who made a mark change or call (nothing when no name was given)."""
    if name:
        db.run("INSERT INTO made_by (id, name) VALUES (?, ?) ON CONFLICT (id) DO NOTHING",
               (row_id, name))


class Unavailable(RuntimeError):
    """No database to use; the message says why, for the page."""


def on_render() -> bool:
    return bool(os.environ.get("RENDER"))


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


class Db:
    def __init__(self, conn: Any, postgres: bool, key: str = "") -> None:
        self.conn = conn
        self.postgres = postgres
        self.key = key          # which database this is (for caches kept per database)
        self.pid = os.getpid()  # the process that opened it (a pooled one never crosses a fork)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def run(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.conn.execute(self._sql(sql), params)

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return list(self.conn.execute(self._sql(sql), params).fetchall())

    def one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator["Db"]:
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

    def many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
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


def rows_for(db: Db, select: str, column: str, ids: Iterable[str], order: str = "") -> list[Row]:
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


def company_id() -> str | None:
    """The company this thread works on (sign-up mode), or None for the base database."""
    return _company.get()


def schema_name(company: str) -> str:
    """The Postgres schema of a company's data: c_ and its id."""
    if not _COMPANY_ID.match(company):
        raise ValueError("not a company id")
    return f"c_{company}"


def set_company(company: str | None) -> contextvars.Token[str | None]:
    """Work on this company's data from now on in this thread (None: the base database);
    give the token to reset_company to go back."""
    if company is not None:
        schema_name(company)            # a bad id never reaches a schema or file name
    return _company.set(company)


def reset_company(token: contextvars.Token[str | None]) -> None:
    _company.reset(token)


@contextmanager
def for_company(company: str | None) -> Iterator[None]:
    """Inside, connect() reaches this company's data (None: the base database, where the
    accounts are)."""
    token = set_company(company)
    try:
        yield
    finally:
        reset_company(token)


_P = ParamSpec("_P")
_T = TypeVar("_T")


def carry(fn: Callable[_P, _T]) -> Callable[_P, _T]:
    """fn, to run in another thread with this thread's company (and everything else kept
    in context variables): a search a company starts saves into that company's data.
    Each call takes its own copy, so one context is never entered by two threads."""
    context = contextvars.copy_context()
    return functools.partial(context.run, fn)


def database_key() -> str:
    """Which database connect() reaches now (the key of the caches kept per database, Db.key)."""
    return _database()[0]


def company_file(company: str) -> Path:
    """A company's own SQLite file (no DATABASE_URL: local use and the tests)."""
    return http.CACHE_DIR / "companies" / f"{company}.db"


def _database() -> tuple[str, bool]:
    """(which database, is it Postgres): the DATABASE_URL, or the local SQLite file's path.
    With a company set (sign-up mode), that company's own: on Postgres the same database
    with the company's schema, on SQLite a file of its own. Raises Unavailable on Render
    without DATABASE_URL."""
    url, company = database_url(), _company.get()
    if url:
        return (url if company is None else f"{url}{_SCHEMA_MARK}{schema_name(company)}"), True
    if on_render():
        raise Unavailable("no database is connected yet (DATABASE_URL is not set in Render)")
    if company is not None:
        return str(company_file(company).resolve()), False
    return str((http.CACHE_DIR / "leadgen.db").resolve()), False


def open_db() -> Db:
    """A Db (autocommit) for one user at a time: an idle one from the pool, else a new
    connection. Give it back with release(db) (or close it with db.conn.close()).

    Raises Unavailable when there is no database.
    """
    key, postgres = _database()
    db = _from_pool(key)
    return db if db is not None else _new_connection(key, postgres)


# Anything in a connection error that could be a password: the user:password@ part of a
# URL, and password=... in key=value form.
_SECRETS = re.compile(r"(\w+://)[^@\s/]*@|(password\s*=\s*)\S+", re.IGNORECASE)


def _reason(exc: BaseException) -> str:
    """Why a connection failed, for the log (e.g. "password authentication failed"),
    on one line and with any password taken out. When several addresses were tried (an
    IPv6 one the server can't reach, then IPv4), the last one's reason is the one kept."""
    text = " ".join(str(exc).split())
    if len(text) > 300:
        text = text[:120] + " ... " + text[-175:]
    return _SECRETS.sub(lambda m: (m.group(1) or m.group(2)) + "***" + ("@" if m.group(1) else ""), text)


def direct_url(url: str) -> str:
    """The database's direct address for a company's connections. Neon's pooled address
    (a host with "-pooler" in it) refuses the search_path start-up option that keeps a
    company to its own schema, so they connect to the same database without the pool."""
    return re.sub(r"((?:@|host=)[^/?:@\s]*?)-pooler(\.)", r"\1\2", url, count=1)


def _new_connection(key: str, postgres: bool) -> Db:
    """A new connection; the first one in this process also creates any missing tables."""
    conn: Any
    url, _, schema = key.partition(_SCHEMA_MARK)
    if postgres:
        try:
            if schema:
                # A company's connections see only its own schema: its tables, never the base's.
                conn = psycopg.connect(direct_url(url), autocommit=True, connect_timeout=15,
                                       options=f"-c search_path={schema}")
            else:
                conn = psycopg.connect(url, autocommit=True, connect_timeout=15)
        except (psycopg.Error, OSError) as exc:
            global last_failure
            log.warning("Database connection failed: %s: %s", exc.__class__.__name__, _reason(exc))
            last_failure = (time.time(), f"{exc.__class__.__name__}: {_reason(exc)}")
            raise Unavailable("the database could not be reached") from exc
    else:
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(key, timeout=30, isolation_level=None, check_same_thread=False)
    db = Db(conn, postgres, key)
    try:
        with _lock:
            # Once per process and database; on SQLite also when the file is new (a
            # deleted cache folder), as the tables went with it.
            if key not in _ready or (not postgres and not _has_tables(db)):
                if schema:
                    db.run(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                _create_tables(db)
                _ready.add(key)
    except Exception:
        conn.close()
        raise
    return db


def _has_tables(db: Db) -> bool:
    return db.one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_found'") is not None


def _has_accounts(db: Db) -> bool:
    return db.one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tokens'") is not None


def with_accounts(db: Db) -> Db:
    """The base database's connection, its account tables (ACCOUNT_SCHEMA) created if
    missing: once per process (accounts.py asks before each use), so AARCO's own database
    gets them only once sign-up mode uses them."""
    if _SCHEMA_MARK in db.key or Path(db.key).parent.name == "companies":
        raise ValueError("the accounts are kept in the base database only")
    marker = db.key + " |accounts"
    if marker in _ready and (db.postgres or _has_accounts(db)):
        return db
    with _lock:
        _create_tables(db, ACCOUNT_SCHEMA)
        _ready.add(marker)
    return db


def _idle_now(db: Db) -> bool:
    """Whether a connection is from this process, open and not inside a transaction."""
    try:
        if db.pid != os.getpid():
            return False
        if db.postgres:
            return (not db.conn.closed and not db.conn.broken
                    and db.conn.info.transaction_status == TransactionStatus.IDLE)
        return not db.conn.in_transaction
    except Exception:  # noqa: BLE001 - a closed SQLite connection raises; not reused
        return False


def _usable(db: Db, idle: float) -> bool:
    """Whether a pooled connection can be handed out: idle (_idle_now) for not too long
    and still answering."""
    if idle > POOL_MAX_IDLE or not _idle_now(db):
        return False
    if idle >= POOL_CHECK_AFTER:
        try:
            db.one("SELECT 1")
        except Exception:  # noqa: BLE001 - sqlite3 or psycopg: a dropped connection
            return False
    return True


def _from_pool(key: str) -> Db | None:
    """The most recently used idle connection, when it is still usable. When it isn't,
    the database has most likely dropped the older ones too, so they are all closed and
    the caller opens a new one, rather than each next request finding a dead one."""
    with _pool_lock:
        idle = _pool.get(key)
        if not idle:
            return None
        db, since = idle.pop()
    if _usable(db, time.monotonic() - since):
        return db
    with _pool_lock:
        rest = [old for old, _ in _pool.pop(key, [])]
    for old in [db, *rest]:
        _close(old)
    return None


def release(db: Db) -> None:
    """Hand a connection from open_db() back: kept for the next user when it is healthy
    and the pool has room, else closed."""
    if _idle_now(db):
        with _pool_lock:
            idle = _pool.setdefault(db.key, [])
            if len(idle) < POOL_SIZE:
                idle.append((db, time.monotonic()))
                return
    _close(db)


def _close(db: Db) -> None:
    try:
        db.conn.close()
    except Exception as exc:  # noqa: BLE001 - sqlite3 or psycopg; it is being dropped
        log.info("Closing a database connection failed: %s", exc.__class__.__name__)


def close_pool() -> None:
    """Close every idle pooled connection (at exit, and in tests)."""
    with _pool_lock:
        idle = [db for dbs in _pool.values() for db, _ in dbs]
        _pool.clear()
    for db in idle:
        _close(db)


atexit.register(close_pool)


def _create_tables(db: Db, schema: Sequence[str] = SCHEMA) -> None:
    for attempt in range(3):
        try:
            for statement in schema:
                db.run(statement)
            return
        except Exception as exc:
            # Two processes creating the same table at once: one of them fails; retry.
            if attempt == 2:
                raise
            log.info("Creating the tables failed (%s); retrying", exc.__class__.__name__)


@contextmanager
def connect() -> Iterator[Db]:
    """A connection (autocommit) for a few statements; raises Unavailable when there
    is no database. Inside scope() every connect() to the same database (the request's
    company's, or the base one with the accounts) shares one connection."""
    shared = getattr(_local, "scope", None)
    if shared is not None:
        key = _database()[0]
        if key not in shared["dbs"]:
            shared["dbs"][key] = open_db()
            shared["opened"] += 1
        yield shared["dbs"][key]
        return
    db = open_db()
    try:
        yield db
    finally:
        release(db)


def begin_scope() -> None:
    """From now on this thread's connect() calls share one connection (opened when
    first needed) until end_scope(): one connection per web request."""
    _local.scope = {"dbs": {}, "opened": 0}


def end_scope() -> int:
    """Give back the shared connections; returns how many were taken (0 or 1 for each
    database the request used)."""
    shared = getattr(_local, "scope", None)
    _local.scope = None
    if not shared:
        return 0
    for db in shared["dbs"].values():
        release(db)
    return int(shared["opened"])


@contextmanager
def scope() -> Iterator[None]:
    begin_scope()
    try:
        yield
    finally:
        end_scope()
