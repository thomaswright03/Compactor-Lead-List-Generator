"""A copy of everything the team has done, written to a file on request, and put back
only on request, adding what is missing and never changing or removing a row.

    python -m leadgen backup                    # writes leadgen-backup-<Utah date and time>.json.gz
    python -m leadgen restore FILE              # says what putting it back would add (changes nothing)
    python -m leadgen restore FILE --apply      # adds the rows the database doesn't have

The file holds every row of every table that keeps the team's work or the site's
records (store.SCHEMA): the saved leads with their source listings, every Yes / No
click and mark, every call with its Conversation Summary, who made each, the
corrected phones and contact names, the search history and its failures, the leads
merged or taken out of the list, the emergency switches, the recent problems and the
Yelp call count. Left out: the cache of map, Google and Yelp answers (temporary, and
their terms limit keeping them) and a running search's checkpoint rows (gone once it
ends). It is read in one transaction, so it is one moment's copy, and written as
gzip-compressed JSON (one file, readable without the site: each table's columns and
rows).

Putting a copy back inserts each row whose key the database doesn't hold yet
(INSERT ... ON CONFLICT DO NOTHING, in one transaction): into a new, empty database
it brings everything back; into the live one it only fills in what is missing. A row
already there is never changed, and nothing is deleted. The site never runs either
command by itself; see the operator runbook's "Backups and restoring".
"""

import gzip
import json
import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
from .localtime import date_time_text, utah

FORMAT = "leadgen-backup"
VERSION = 1
# Not copied: answers from the map servers, Google and Yelp (they expire within days and
# their terms limit keeping them), and a running search's checkpoints (interrupted.py).
SKIPPED = ("cache", "search_runs", "search_found")


def _primary_keys() -> dict[str, tuple[str, ...]]:
    """{table: its key columns}, read from store.SCHEMA (so a new table is copied too)."""
    keys: dict[str, tuple[str, ...]] = {}
    for statement in store.SCHEMA:
        made = re.match(r"\s*CREATE TABLE IF NOT EXISTS (\w+) \((.*)\)\s*$", statement, re.S)
        if not made:
            continue
        table, body = made.groups()
        composite = re.search(r"PRIMARY KEY \(([^)]*)\)", body)
        single = re.search(r"(\w+) \w+ PRIMARY KEY", body)
        if composite:
            keys[table] = tuple(c.strip() for c in composite.group(1).split(","))
        elif single:
            keys[table] = (single.group(1),)
    return keys


KEYS = _primary_keys()
TABLES = tuple(t for t in KEYS if t not in SKIPPED)


class BackupError(ValueError):
    """A backup file that can't be put back; the message says why, in plain words."""


@dataclass
class TableCount:
    """One table in a restore: rows in the file, already in the database, added."""

    table: str
    in_file: int
    already: int
    added: int


def _columns(db: store.Db, table: str) -> list[str]:
    cursor = db.conn.execute(f"SELECT * FROM {table} WHERE 1 = 0")
    return [getattr(d, "name", None) or d[0] for d in cursor.description]


def _snapshot(db: store.Db) -> dict[str, Any]:
    """Every row of TABLES, read in one transaction (one moment's copy)."""
    tables: dict[str, Any] = {}
    with db.transaction():
        if db.postgres:
            # Every table as it was when the copy started, whatever is saved meanwhile.
            db.run("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        for table in TABLES:
            columns = _columns(db, table)
            order = ", ".join(KEYS[table])
            rows = db.all(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}")
            tables[table] = {"columns": columns, "rows": [list(r) for r in rows]}
    return tables


def summary(tables: dict[str, Any]) -> dict[str, int]:
    """What the pages count, from a copy's (or the database's) tables: saved leads, Yes /
    No marks, calls and searches, so a restore can be checked against the copy."""
    def rows(table: str) -> list[list[Any]]:
        return list(tables.get(table, {}).get("rows", []))

    def column(table: str, name: str) -> int:
        return list(tables.get(table, {}).get("columns", [])).index(name)

    leads = [r for r in rows("leads") if r[column("leads", "lead")] not in (None, "null")]
    marks = rows("marks")
    return {"saved leads": len(leads),
            "marked yes": sum(1 for r in marks if r[column("marks", "value")] == "yes"),
            "marked no": sum(1 for r in marks if r[column("marks", "value")] == "no"),
            "calls": len(rows("calls")),
            "searches": len(rows("searches")) + len(rows("search_failures"))}


def write(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    """Write a copy of the database (DATABASE_URL, or the local SQLite file) to `path`
    (default leadgen-backup-<Utah date and time>.json.gz here); returns (the path, the
    copy). Never overwrites a file. Raises store.Unavailable without a database."""
    made = time.time()
    if path is None:
        path = Path(f"leadgen-backup-{utah(made):%Y-%m-%d-%H%M}.json.gz")
    with store.connect() as db:
        tables = _snapshot(db)
        postgres = db.postgres
    copy = {"format": FORMAT, "version": VERSION, "made_at": made,
            "made": date_time_text(made) + " (Utah time)",
            "database": "postgres" if postgres else "sqlite",
            "counts": summary(tables), "tables": tables}
    data = json.dumps(copy, separators=(",", ":")).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create: an existing file (an earlier copy) is never replaced.
    with open(path, "xb") as f:
        f.write(gzip.compress(data) if path.suffix == ".gz" else data)
    return path, copy


def read(path: Path) -> dict[str, Any]:
    """A backup file's copy (gzip-compressed or plain JSON). Raises BackupError for a
    file that isn't one."""
    try:
        raw = path.read_bytes()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        copy = json.loads(raw)
    except (OSError, ValueError, EOFError) as exc:
        raise BackupError(f"{path} couldn't be read as a backup ({exc.__class__.__name__}). If it ends "
                          "in .gpg, decrypt it first (operator runbook, Backups and restoring).") from exc
    if not isinstance(copy, dict) or copy.get("format") != FORMAT:
        raise BackupError(f"{path} is not a Lead Finder backup.")
    if copy.get("version") != VERSION:
        raise BackupError(f"{path} is a backup of version {copy.get('version')}; this copy of the "
                          f"code reads version {VERSION}. Update the code first.")
    unknown = sorted(set(copy.get("tables") or {}) - set(TABLES))
    if unknown:
        raise BackupError(f"{path} has tables this copy of the code doesn't know ({', '.join(unknown)}): "
                          "it was made by a newer version. Update the code first.")
    return copy


def _keys_in(db: store.Db, table: str) -> set[tuple[Any, ...]]:
    return {tuple(r) for r in db.all(f"SELECT {', '.join(KEYS[table])} FROM {table}")}


def _plan(db: store.Db, copy: dict[str, Any]) -> list[tuple[str, list[str], list[list[Any]], TableCount]]:
    """For each table in the copy: its columns, the rows the database lacks, and the counts."""
    plan = []
    for table in TABLES:
        part = copy["tables"].get(table)
        if not part:
            continue
        columns = list(part["columns"])
        missing = [c for c in columns if c not in _columns(db, table)]
        if missing:
            raise BackupError(f"The database's {table} table has no {', '.join(missing)} column: the "
                              "backup was made by a newer version. Update the code first.")
        at = [columns.index(k) for k in KEYS[table]]
        have = _keys_in(db, table)
        new, seen = [], set()
        for row in part["rows"]:
            key = tuple(row[i] for i in at)
            if key not in have and key not in seen:
                seen.add(key)
                new.append(row)
        plan.append((table, columns, new,
                     TableCount(table, len(part["rows"]), len(part["rows"]) - len(new), len(new))))
    return plan


def restore(copy: dict[str, Any], apply: bool = False) -> list[TableCount]:
    """Put a copy back into the database (DATABASE_URL, or the local SQLite file): with
    apply, every row whose key isn't there yet is inserted, all in one transaction;
    without, nothing is written. A row already there is never changed and nothing is
    removed. Returns each table's counts (added: what was, or would be, added)."""
    with store.connect() as db:
        if not apply:
            return [count for *_, count in _plan(db, copy)]
        with db.transaction():
            plan = _plan(db, copy)
            for table, columns, rows, _ in plan:
                _insert(db, table, columns, rows)
    return [count for *_, count in plan]


def _insert(db: store.Db, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    sql = (f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
           "ON CONFLICT DO NOTHING")
    if db.postgres:
        with db.conn.cursor() as cur:
            cur.executemany(db._sql(sql), rows)
    else:
        db.conn.executemany(sql, rows)


def counts_now() -> dict[str, int]:
    """summary() of the database as it is now (to compare with a copy after a restore)."""
    with store.connect() as db:
        tables = {t: {"columns": _columns(db, t), "rows": db.all(f"SELECT * FROM {t}")}
                  for t in ("leads", "marks", "calls", "searches", "search_failures")}
    return summary(tables)


def size_text(path: Path) -> str:
    size = os.path.getsize(path)
    return f"{size / 1e6:.1f} MB" if size >= 1e6 else f"{max(1, round(size / 1e3))} KB"
